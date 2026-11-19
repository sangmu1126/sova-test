"""Bind a public-only qualification task to inspected registry image digests.

The native restrictive template remains marked not-executable. The final
Harbor qualification task adds one explicit artifact transfer, has its own
hash, and is not a production sandbox admission or a promotion approval.
"""
import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil

from sova.adapters.sandbox.harbor import prepare_task
from sova.domain.contracts import ResourceScope, canonical_json
from sova.domain.sandbox import SandboxRequest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('prepared','publication','output'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args()
    os.umask(0o077)
    prepared=json.loads((a.prepared/'preparation.json').read_text())
    published=json.loads(a.publication.read_text())
    if prepared['classification']!='public-source-only' or published['registry_credentials_removed'] is not True:
        raise ValueError('Public exercise and removed registry credentials required')
    for name in ('agent','verifier'):
        if not all(published['images'][name][f] is True for f in ('registry_readback_verified','actual_runtime_reference_verified')):
            raise ValueError('Actual registry/runtime image identity required')
    now=datetime.now(UTC)
    tests=prepared['contexts']['verifier']
    request=SandboxRequest(ResourceScope('personal-qualification','public-boundary-exercise','local-docker','request-boundary'), 'public-boundary-'+now.strftime('%Y%m%d%H%M%S'),prepared['buggy_sha256'],hashlib.sha256(canonical_json(tests).encode()).hexdigest(), published['images']['agent']['reference'],published['images']['verifier']['reference'],180,now+timedelta(minutes=25))
    template=prepare_task(request,now=now)
    a.output.mkdir(mode=0o700,exist_ok=False)
    task=a.output/'task'
    shutil.copytree(a.prepared/'task',task)
    shutil.copyfile(task/'environment/docker-compose.yaml',task/'tests/docker-compose.yaml')
    config=template['task_toml'].replace('schema_version = "1.4"\n','schema_version = "1.4"\nartifacts = ["/workspace/http_boundary.py"]\n',1)
    normalize = "import os,stat;f=os.open('/workspace/http_boundary.py',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);s=os.fstat(f);assert stat.S_ISREG(s.st_mode) and s.st_uid==10001 and 0<s.st_size<=16384;os.fchmod(f,0o644);os.close(f)"
    import shlex
    config += "\n[[verifier.collect]]\ncommand = " + json.dumps("python -c " + shlex.quote(normalize)) + "\ntimeout_sec = 5\nuser = \"10001:10001\"\n"
    (task/'task.toml').write_text(config)
    # Only these verified public task inputs become readable in non-root guests.
    for path in task.rglob('*'):
        if path.is_symlink():
            raise ValueError("Public task must not contain links")
        path.chmod(0o755 if path.is_dir() else (0o555 if path.suffix == ".sh" else 0o444))
    task.chmod(0o755)
    result={'classification':'public-source-only','native_preparation':template,'final_task_configuration_sha256':hashlib.sha256(config.encode()).hexdigest(),'artifact_transfer':['/workspace/http_boundary.py'],'separate_bundled_verifier':True,'model_called':False,'agent_executed':False,'production_admitted':False,'promotion_approved':False,'files_sha256':{str(x.relative_to(task)):hashlib.sha256(x.read_bytes()).hexdigest() for x in task.rglob('*') if x.is_file()},'file_modes':{str(x.relative_to(task)):oct(x.stat().st_mode & 0o777) for x in task.rglob('*')}}
    (a.output/'binding.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'task_prepared':True,'production_admitted':False,'model_called':False,'task_configuration':result['final_task_configuration_sha256']}))


if __name__=='__main__':
    main()
