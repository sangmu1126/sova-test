"""RabbitMQ queue handoff world for the provider-neutral conformance kit.

Observed independently of the adapter, and never by reading a message (that
would change it): content is the queue's ready count as AMQP reports it now,
access is the raw permission entries, a write attempt is a real publish as
the user inside a transaction that is always rolled back, and the write path
that outlives any client is a shovel delivering into the source.
"""

from urllib.parse import quote

from rabbitmq_business import EXCHANGE, QUEUE, ROUTING_KEY

from sova.adapters.rabbitmq_queue_handoff import RabbitMqQueueHandoffAdapter


class Route:
    def __init__(self, vhost):
        self.vhost = vhost

    def get(self):
        return self.vhost

    def set(self, vhost):
        self.vhost = vhost


class RabbitMqWorld:
    def __init__(self, business):
        self.b, self.vhosts, self.users = business, business["vhosts"], business["users"]
        self.writers = {"app": self.users["app"], "other": self.users["batch"]}
        self._route = Route(self.vhosts["src"])
        self.pending = None

    def _vhost(self, side):
        return self.vhosts["src" if side == "source" else "tgt"]

    def _broker(self, side):
        return {"http": self.b["http"], "auth": self.b["admin"],
                "amqp": lambda vhost: self.b["params"](vhost)}

    def _adapter(self, source, target):
        return RabbitMqQueueHandoffAdapter(
            "rabbitmq-queue-handoff", broker=self._broker, source_vhost=self._vhost(source),
            target_vhost=self._vhost(target), queue=QUEUE, exchange=EXCHANGE,
            routing_key=ROUTING_KEY, publishers=(self.users["app"], self.users["batch"]),
            app_publisher=self.users["app"], consumers=(self.users["reader"],),
            route=self._route, admin_user=self.b["admin"][0])

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    def _amqp(self, vhost, user=None):
        import pika

        return pika.BlockingConnection(self.b["params"](vhost, user))

    def can_write(self, side, writer):
        import pika

        try:
            connection = self._amqp(self._vhost(side), self.writers[writer])
        except pika.exceptions.AMQPConnectionError:
            return False  # no access to the virtual host at all
        try:
            channel = connection.channel()
            channel.tx_select()
            channel.basic_publish(EXCHANGE, ROUTING_KEY, b"sova-probe")
            channel.tx_rollback()  # the publish is checked; nothing stays
            return True
        except pika.exceptions.ChannelClosedByBroker as error:
            if error.reply_code in (403, 404):
                return False
            raise
        finally:
            if connection.is_open:
                connection.close()

    def business_write(self, count):
        import pika

        connection = self._amqp(self._vhost("source"), self.users["app"])
        channel = connection.channel()
        channel.confirm_delivery()
        for i in range(count):
            channel.basic_publish(EXCHANGE, ROUTING_KEY, f'{{"late": {i}}}'.encode(),
                                  pika.BasicProperties(delivery_mode=2))
        connection.close()

    def business_delete(self, count):
        connection = self._amqp(self._vhost("source"), self.users["reader"])
        channel = connection.channel()
        for _ in range(count):
            method, _, _ = channel.basic_get(QUEUE, auto_ack=False)
            channel.basic_ack(method.delivery_tag)
        connection.close()

    def content(self, side):
        import pika

        vhost = self._vhost(side)
        if vhost not in {v["name"] for v in self.b["api"]("GET", "/vhosts") or []}:
            return None
        try:
            connection = self._amqp(vhost)
        except pika.exceptions.AMQPConnectionError:
            return None
        try:
            return connection.channel().queue_declare(QUEUE, passive=True).method.message_count
        except pika.exceptions.ChannelClosedByBroker:
            return None
        finally:
            if connection.is_open:
                connection.close()

    def _access(self, side):
        found = self.b["api"]("GET", "/permissions") or []
        return sorted((p["user"], p["configure"], p["write"], p["read"]) for p in found
                      if p["vhost"] == self._vhost(side) and p["user"] in self.users.values())

    def _shovels(self):
        return sorted(s["name"] for s in self.b["api"]("GET", "/parameters/shovel") or [])

    def state(self):
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), self._shovels(), self._route.get())

    def reopen_source(self):
        """The source's permissions restored as they were before the handoff."""
        configure, write, read = self.b["grants"]["app"]
        self.b["api"]("PUT", f"/permissions/{quote(self._vhost('source'), safe='')}/"
                             f"{self.users['app']}",
                      {"configure": configure, "write": write, "read": read})

    def tamper_target(self):
        connection = self._amqp(self._vhost("target"))
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish("", QUEUE, b'{"forged": true}')
        connection.close()

    def move_route(self, side):
        self._route.set(self._vhost(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._vhost(side)

    def open_pending_write(self):
        """A shovel feeding the source exchange: it writes with no client open."""
        source = quote(self._vhost("source"), safe="")
        user, password = self.b["admin"]
        uri = f"amqp://{user}:{password}@localhost:5672/{source}"
        self.b["api"]("PUT", f"/queues/{source}/feeder", {"durable": True})
        self.pending = "sova-feeder"
        self.b["api"]("PUT", f"/parameters/shovel/{source}/{self.pending}", {"value": {
            "src-protocol": "amqp091", "src-uri": uri, "src-queue": "feeder",
            "dest-protocol": "amqp091", "dest-uri": uri, "dest-exchange": EXCHANGE}})

    def resolve_pending_write(self):
        source = quote(self._vhost("source"), safe="")
        self.b["api"]("DELETE", f"/parameters/shovel/{source}/{self.pending}")

