import json, os, shutil, sys, urllib.request, urllib.error

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

with open('site.zip', 'rb') as f:
    data = f.read()

req = urllib.request.Request(
    f'https://api.netlify.com/api/v1/sites/{site_id}/deploys',
    data=data,
    headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/zip',
    },
    method='POST',
)
try:
    resp = urllib.request.urlopen(req)
    r = json.load(resp)
    d = r.get('deploy', r)
    print('state:', d.get('state'))
    print('published:', d.get('published'))
    print('url:', d.get('ssl_url') or d.get('url'))
    print('created:', d.get('created_at'))
    print('files:', len(d.get('files', {})))
    if d.get('state') != 'ready':
        print('FATAL: deploy state is not ready')
        sys.exit(1)
except urllib.error.HTTPError as e:
    print(f'HTTP Error: {e.code} {e.reason}')
    print(e.read().decode(errors='replace'))
    sys.exit(1)
