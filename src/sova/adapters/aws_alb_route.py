"""Route store on an AWS Application Load Balancer: one listener forwards to one side.

A data adapter's ``commit_route`` writes to a route store (``get``/``set``).
``AlbListenerRoute`` keeps one listener's default action forwarding all of its
traffic to the target group of one side, and answers ``get`` from both the
configuration and what the load balancer really serves:

* ``set`` reads the listener, refuses if its default action is anything but a
  plain forward to one of the registered sides (someone else's rule is never
  overwritten by a switch), and replaces it with a forward to the wanted side.
  ELBv2 offers no conditional write, so an edit by someone else between the
  read and the write can be lost; the readback then sees the configuration
  and the answers disagree with the plan and reports no side;
* ``get`` returns a side only if the listener forwards 100% of the traffic to
  that side's target group and every one of ``samples`` requests through the
  load balancer was answered by that side (``observe`` is the caller's probe
  of who answered: a marker the application serves, never a guess).

Connections already open keep their target until they close; the source stays
fenced meanwhile, so a late request through an old connection is refused.
"""

import re
import time

from sova.domain.contracts import ContractError

_ARN = re.compile(r"arn:aws[a-z-]*:elasticloadbalancing:[a-z0-9-]+:\d{12}:"
                  r"(listener|targetgroup)/[A-Za-z0-9/._-]{1,200}")


class AlbListenerRoute:
    def __init__(self, elbv2, *, listener_arn, sides, observe, samples=6,
                 sync_wait_seconds=120.0):
        """``sides`` maps a route value to a target group ARN; ``observe()``
        returns the side marker of one request through the load balancer."""
        if not callable(getattr(elbv2, "modify_listener", None)):
            raise ContractError("ELBv2 client required")
        if type(listener_arn) is not str or not _ARN.fullmatch(listener_arn) or \
                ":listener/" not in listener_arn:
            raise ContractError("Listener ARN required")
        if type(sides) is not dict or len(sides) < 2 or len(set(sides.values())) != len(sides):
            raise ContractError("A distinct target group for each side required")
        for arn in sides.values():
            if type(arn) is not str or not _ARN.fullmatch(arn) or ":targetgroup/" not in arn:
                raise ContractError("Target group ARN required")
        if not callable(observe) or type(samples) is not int or not 1 <= samples <= 50:
            raise ContractError("An observation probe and a bounded sample count required")
        self.elb, self.listener, self.sides = elbv2, listener_arn, dict(sides)
        self.observe_one, self.samples = observe, samples
        self.sync_wait_seconds = sync_wait_seconds

    def _listener(self):
        found = self.elb.describe_listeners(ListenerArns=[self.listener])["Listeners"]
        if len(found) != 1:
            raise ContractError("Registered listener not found")
        return found[0]

    def _configured(self, listener):
        """The side the default action forwards everything to, or None."""
        actions = listener.get("DefaultActions", ())
        if len(actions) != 1 or actions[0].get("Type") != "forward":
            return None, actions
        action = actions[0]
        groups = [g for g in (action.get("ForwardConfig") or {}).get("TargetGroups", ())
                  if g.get("Weight", 1) > 0]
        arns = ([g["TargetGroupArn"] for g in groups] if groups
                else [action.get("TargetGroupArn")])
        if len(arns) != 1:
            return None, actions
        return next((ref for ref, arn in self.sides.items() if arn == arns[0]), None), actions

    def observe(self):
        configured, _ = self._configured(self._listener())
        answers = []
        for _ in range(self.samples):
            try:
                answers.append(self.observe_one())
            except OSError:
                answers.append(None)
        served = answers[0] if answers and len(set(answers)) == 1 else None
        return {"route": configured if served == configured else None,
                "configured": configured, "answers": answers}

    def get(self):
        return self.observe()["route"]

    def set(self, ref):
        if ref not in self.sides:
            raise ContractError("Unknown route side")
        listener = self._listener()
        configured, actions = self._configured(listener)
        if configured is None and actions:
            raise ContractError("The listener does something else; route not switched")
        if configured != ref:
            self.elb.modify_listener(ListenerArn=self.listener, DefaultActions=[{
                "Type": "forward", "ForwardConfig": {"TargetGroups": [
                    {"TargetGroupArn": self.sides[ref], "Weight": 1}]}}])
        deadline = time.monotonic() + self.sync_wait_seconds
        while time.monotonic() < deadline and self.get() != ref:
            time.sleep(2)
