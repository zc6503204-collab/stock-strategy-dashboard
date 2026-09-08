"""Reproduce this project's reviewed official bundle; never replace an existing credential."""
from pathlib import Path,PurePosixPath
from urllib.request import urlretrieve
import hashlib,zipfile,stat

ROOT=Path(__file__).resolve().parents[1]
URL='https://apicdn1.app.gtht.com/web2/jh-static-gtht-skills/gtht-skills.zip'
SHA256='352d92bf630a84ba57370080ef278148484634306704be1d48e563f839abaf80'

def main():
    private=ROOT/'.local';private.mkdir(exist_ok=True);private.chmod(0o700)
    archive=private/'downloads/gtht-skills.zip';archive.parent.mkdir(exist_ok=True)
    if not archive.exists():urlretrieve(URL,archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA256:
        raise SystemExit('官方包内容已变更；请先审阅新版，当前安装未改动。')
    target=private/'vendor/gtht';target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            path=PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or stat.S_ISLNK(info.external_attr>>16):
                raise SystemExit('压缩包包含不允许的路径。')
            if path.name=='gtht-entry.json':continue
            z.extract(info,target)
    shared=target/'gtht-skill-shared';shared.mkdir(exist_ok=True);shared.chmod(0o700)
    print('灵犀官方技能已安装在本项目；已有凭证保持不变。')

if __name__=='__main__':main()
