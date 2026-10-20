"""Read-only HTTP/navigation audit in a fresh synthetic database; zero model calls."""
import argparse
import copy
import json
import tempfile
import threading
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from cashflow_copilot import web,agent_web,site_web,ui
from cashflow_copilot.cli import cmd_seed
from cashflow_copilot.store import SQLiteStore


class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=set()
    def handle_starttag(self,tag,attrs):
        href=dict(attrs).get('href','')
        if tag=='a' and href.startswith('/') and not href.startswith('//'):
            self.links.add(href.split('#')[0])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Use a fresh report path; previous evidence is preserved.')
    agent_web.mount(web)
    with tempfile.TemporaryDirectory(prefix='fluxora-audit-') as folder:
        original_db=web.DB_PATH
        web.DB_PATH=str(Path(folder)/'audit.sqlite')
        cmd_seed(web.DB_PATH)
        before=web.read_state(lambda l:copy.deepcopy(vars(l)))
        server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        checked={};queue={'/welcome','/product','/how-it-works','/trust','/about','/docs','/app','/forecast','/workspace/features'}
        queue.update('/docs/'+k for k in site_web.DOCS)
        for _,rows in ui.GROUPS:
            for url,_,_,children in rows:queue.add(url);queue.update(p for p,_ in children)
        try:
            # Only follow same-origin GET links, never forms or external destinations.
            while queue:
                path=sorted(queue)[0];queue.remove(path)
                if path in checked:continue
                if len(checked)>180:raise AssertionError('Unexpected link expansion')
                with urllib.request.urlopen(base+path,timeout=15) as response:
                    data=response.read();ctype=response.headers['Content-Type']
                    checked[path]=dict(status=response.status,content_type=ctype)
                    if 'text/html' in ctype:
                        text=data.decode()
                        if not response.headers.get('Content-Disposition') and not path.startswith('/decision-brief/print'):
                            assert '<main id="main"' in text,path
                        links=Links();links.feed(text)
                        queue.update(p for p in links.links if p and p not in checked)
            after=web.read_state(lambda l:copy.deepcopy(vars(l)))
            assert before==after,'GET navigation changed financial domain state'
            result=dict(status='passed',http_gets=len(checked),pages=checked,financial_domain_unchanged=True,
                external_model_calls=0,provider_stub_calls=0,independent_human_participants=0,
                scope='Fresh synthetic HTTP/navigation audit; not a browser/mobile visual or live-model test')
        finally:
            server.shutdown();server.server_close();thread.join(5);web.DB_PATH=original_db
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='pages'}))


if __name__=='__main__':main()
