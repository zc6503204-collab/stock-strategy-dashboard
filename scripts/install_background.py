"""Install the local app's login service and notification helper, never an AI job."""
from pathlib import Path
import os,plistlib,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
app=ROOT/'.local/ShortlistNotifier.app';contents=app/'Contents';binary=contents/'MacOS/ShortlistNotifier'
binary.parent.mkdir(parents=True,exist_ok=True)
info={'CFBundleIdentifier':'local.shortlist.notifications','CFBundleName':'短线观察台','CFBundleDisplayName':'短线观察台',
      'CFBundleExecutable':'ShortlistNotifier','CFBundlePackageType':'APPL','CFBundleVersion':'2','LSUIElement':True}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['xcrun','swiftc',str(ROOT/'scripts/ShortlistNotifier.swift'),'-o',str(binary),'-framework','AppKit','-framework','UserNotifications'],check=True)
subprocess.run(['codesign','--force','--sign','-',str(app)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
# Resolve installed runtime executables rather than relying on a login shell's PATH.
import shutil
extra=[str(Path(shutil.which(x)).parent) for x in ['node','longbridge'] if shutil.which(x)]
path=':'.join(dict.fromkeys(extra+['/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin']))
agent=Path.home()/'Library/LaunchAgents/local.shortlist.dashboard.plist';agent.parent.mkdir(parents=True,exist_ok=True)
config={'Label':'local.shortlist.dashboard','ProgramArguments':[str(ROOT/'.venv/bin/python'),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8765','--no-access-log','--timeout-graceful-shutdown','3'],
        'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
        'EnvironmentVariables':{'PATH':path,'PYTHONUNBUFFERED':'1'},'StandardOutPath':str(ROOT/'.local/server.log'),
        'StandardErrorPath':str(ROOT/'.local/server.log'),'ProcessType':'Background'}
agent.write_bytes(plistlib.dumps(config));agent.chmod(0o600)
print('本机通知助手与登录启动配置已生成；启动服务需使用 launchctl bootstrap。')
