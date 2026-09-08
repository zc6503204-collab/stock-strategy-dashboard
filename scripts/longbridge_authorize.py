"""Keep an interactive SDK authorization wait outside the web-service process."""
import json,sys
from pathlib import Path
from longbridge.openapi import OAuthBuilder

def emit(value):
    print(json.dumps(value),flush=True)

def callback(url):
    if '--cached' in sys.argv:raise RuntimeError('interactive authorization required')
    emit({'auth_url':url})

try:
    client=json.loads((Path(__file__).resolve().parents[1]/'.local/longbridge-client.json').read_text())
    oauth=OAuthBuilder(client['client_id']).build(callback)
    emit({'authorized':True})
except Exception:
    emit({'error':'长桥授权未完成，请重试'})
    raise SystemExit(1)
