import json, os, shutil, sys, time, urllib.request, urllib.error

os.chdir('_site')
shutil.make_archive('../site', 'zip', '.')
os.chdir('..')
size = os.path.getsize('site.zip')
print(f'site.zip: {size} bytes')

token = os.environ.get('NETLIFY_AUTH_TOKEN', '')
site_id = os.environ.get('NETLIFY_SITE_ID', '')
if not token:
    print('FATAL: NETLIFY_AUTH_TOKEN is empty or not set')
    sys.exit(1)
if not site_id:
    print('FATAL: NETLIFY_SITE_ID is empty or not set')
    sys.exit(1)

headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/zip'}

with open('site.zip', 'rb') as f:
    data = f.read()

req = urllib.request.Request(
    f'https://api.netlify.com/api/v1/sites/{site_id}/deploys',
    data=data, headers=headers, method='POST',
)
try:
    resp = urllib.request.urlopen(req)
    r = json.load(resp)
    d = r.get('deploy', r)
    deploy_id = d.get('id') or d.get('deploy_id', '')
    print(f'deploy_id: {deploy_id}')
    print(f'initial state: {d.get("state")}')
except urllib.error.HTTPError as e:
    print(f'HTTP Error: {e.code} {e.reason}')
    print(e.read().decode(errors='replace'))
    sys.exit(1)

if not deploy_id:
    print('FATAL: no deploy_id in response')
    sys.exit(1)

for attempt in range(30):
    time.sleep(2)
    try:
        req = urllib.request.Request(
            f'https://api.netlify.com/api/v1/sites/{site_id}/deploys/{deploy_id}',
            headers=headers, method='GET',
        )
        resp = urllib.request.urlopen(req)
        r = json.load(resp)
        d = r.get('deploy', r)
        s = d.get('state', '')
        print(f'  poll {attempt+1}: state={s}  files={len(d.get("files", {}))}')
        if s == 'ready':
            print('state: ready')
            print('published:', d.get('published'))
            print('url:', d.get('ssl_url') or d.get('url'))
            print('created:', d.get('created_at'))
            print('files:', len(d.get('files', {})))
            sys.exit(0)
        if s in ('error', 'failed'):
            print(f'FATAL: deploy {s}')
            sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f'  poll {attempt+1}: HTTP Error: {e.code} {e.reason}')
        sys.exit(1)

print('FATAL: deploy did not become ready after 60s')
sys.exit(1)
