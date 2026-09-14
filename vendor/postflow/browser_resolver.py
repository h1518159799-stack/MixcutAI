import os
from pathlib import Path

def resolve_browser():
    postflow = Path(__file__).resolve().parent
    app = postflow.parent.parent
    candidates = [(app / 'runtime' / 'browser' / 'chromium' / 'chrome.exe', 'embedded_chromium'), (app / 'browser' / 'chromium' / 'chrome.exe', 'embedded_chromium')]
    candidates += [(p, 'playwright_bundled') for p in (postflow / 'ms-playwright').glob('**/chrome.exe')]
    candidates += [(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'), 'system'), (Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'), 'system')]
    local = os.environ.get('LOCALAPPDATA')
    if local: candidates.append((Path(local) / 'Google/Chrome/Application/chrome.exe', 'system'))
    for path, source in candidates:
        if path.is_file():
            return str(path), source
    raise RuntimeError('BROWSER_RUNTIME_NOT_FOUND')
