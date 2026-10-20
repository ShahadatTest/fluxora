"""Read-only browser layout check. Optional dependency: playwright + Chromium."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', type=Path, default=Path('work/browser-evidence'))
    parser.add_argument('--browser-executable')
    parser.add_argument('--draft-plan', action='append', default=[],
        help='Optional eligible plan ID whose draft review should also be checked.')
    args = parser.parse_args()
    if not args.base_url.startswith('http://127.0.0.1:'):
        parser.error('Use the local sandbox loopback URL')
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **(
            {'executable_path': args.browser_executable} if args.browser_executable else {}))
        page = browser.new_page()
        for width, height in [(360, 800), (390, 844), (1440, 900)]:
            page.set_viewport_size({'width': width, 'height': height})
            routes = ['/', '/ledger', '/plans', '/actions', '/demo', '/agent',
                      '/stress', '/today', '/plans/examples', '/facts', '/receipts', '/receipts/inbox', '/daily']
            routes += ['/plans/' + plan + '/draft' for plan in args.draft_plan]
            for route in routes:
                response = page.goto(args.base_url + route)
                assert response.status == 200, route
                assert page.locator('h1').count() == 1, route
                dimensions = page.evaluate('''() => ({
                    viewport: innerWidth,
                    document: document.documentElement.scrollWidth
                })''')
                results.append({'page': route, **dimensions})
                name = route.strip('/').replace('/', '-') or 'overview'
                page.screenshot(path=str(args.output / f'{name}-{width}.png'), full_page=True)
        browser.close()
    (args.output / 'layout-checks.json').write_text(json.dumps(results, indent=2))
    failures = [r for r in results if r['document'] > r['viewport']]
    print(json.dumps({'checks': len(results), 'overflow_failures': failures}))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
