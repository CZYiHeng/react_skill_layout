import glob
f = sorted(glob.glob('g:/react-agent/web/dist/assets/*.js'))[-1]
lines = open(f, encoding='utf-8', errors='ignore').read().split('\n')
for token in ['react-agent:session', 'SettingsModal', 'Sidebar', 'config.model', 'api/config', 'createRoot', 'react-agent-web']:
    for i, ln in enumerate(lines, 1):
        if token in ln:
            print(f'{token!r} first at LINE {i}')
            break
print('TOTAL LINES:', len(lines))
