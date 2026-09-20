"""Build the pinned authentication bridge locally; never handles credentials."""
import io
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.runtime'
REV='e5211d6fd9507c45ade787ede6a9d64596327fbe'
SOURCE=STATE/'ipatool-source'

def main():
    os.umask(0o077)
    if not SOURCE.exists():
        from fetch_sources import fetch_file
        revision=json.loads((ROOT/'dependencies/ipatool.json').read_text())
        if revision['commit']!=REV:raise RuntimeError('ipatool pinned revision mismatch')
        data=fetch_file(f'https://codeload.github.com/majd/ipatool/tar.gz/{REV}',
                        STATE/'upstream/ipatool.tar.gz',revision['sha256'],limit=16*1024*1024)
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            prefix=archive.getmembers()[0].name+'/'
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith(prefix):continue
                name=member.name[len(prefix):]
                if '..' in Path(name).parts:raise ValueError('unsafe archive path')
                target=SOURCE/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(member).read())
    # Keep Apple/Unicorn asset caches inside this project, without changing HOME.
    for relative in ['internal/sap/assets/assets.go','internal/sap/assets/storeagent.go','internal/sap/unicorn/cache_runtime.go']:
        target=SOURCE/relative
        text=target.read_text().replace(':= os.UserCacheDir()', ':= study97CacheDir()')
        if 'func study97CacheDir()' not in text and relative!='internal/sap/assets/storeagent.go':
            text+='\nfunc study97CacheDir() (string, error) { if root := os.Getenv("STUDY97_SAP_CACHE"); root != "" { return root, nil }; return os.UserCacheDir() }\n'
        target.write_text(text)
    dest=SOURCE/'cmd/study97-auth';dest.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'bridge/ipatool-auth.go',dest/'main.go')
    shutil.copyfile(ROOT/'bridge/ipatool-auth_test.go',dest/'main_test.go')
    shutil.copyfile(ROOT/'bridge/ipatool-download-info.go',SOURCE/'pkg/appstore/study97_download_info.go')
    env=dict(os.environ);env.update({'GOMODCACHE':str(STATE/'go-modules'),'GOCACHE':str(STATE/'go-build'),'GOPROXY':'https://proxy.golang.org,direct','GOSUMDB':'sum.golang.org'})
    go=shutil.which('go')
    if not go:raise RuntimeError('需要Go1.25+编译认证桥接器')
    subprocess.run([go,'build','-o',str(STATE/'ipatool-auth'),'./cmd/study97-auth'],cwd=SOURCE,env=env,check=True)
    subprocess.run([go,'test','./cmd/study97-auth'],cwd=SOURCE,env=env,check=True)
    (STATE/'IPATOOL-REVISION').write_text(REV+'\n')
    digest=hashlib.sha256((ROOT/'bridge/ipatool-auth.go').read_bytes()+Path(__file__).read_bytes()+(ROOT/'bridge/ipatool-download-info.go').read_bytes()).hexdigest()
    (STATE/'IPATOOL-BRIDGE-SHA256').write_text(digest+'\n')
    print('Authentication bridge built:',STATE/'ipatool-auth')

if __name__=='__main__':main()
