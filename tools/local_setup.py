"""Offline source launcher. No pip packages, secrets file or network bootstrap."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parents[1]


def check(root=ROOT):
    if sys.version_info<(3,10):raise ValueError('Install Python 3.10 or newer, then run setup again.')
    for name in ['cashflow_copilot/web.py','cashflow_copilot/data/fixture_v1.json',
        'cashflow_copilot/static/fluxora.css','cashflow_copilot/static/fluxora.js','cashflow_copilot/static/fluxora-mark-v1.png']:
        if not (root/name).is_file():raise ValueError('Extract the complete source folder before starting.')
    from cashflow_copilot.agent_skills import validate_registry
    from cashflow_copilot.demo_examples import EXAMPLES,seed
    assert validate_registry()
    for name in EXAMPLES:seed(name)
    return dict(status='ready',python='.'.join(map(str,sys.version_info[:3])),runtime_dependencies='Python standard library only',
        bundled_skills=6,synthetic_examples=len(EXAMPLES),model_calls=0,telegram_calls=0)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Set up and launch the complete local Fluxora source offline.')
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--db',default='.runtime/local/state.sqlite')
    parser.add_argument('--example',choices=['atlas','studio','retail'],default='atlas')
    parser.add_argument('--telegram',action='store_true')
    parser.add_argument('--open-browser',action='store_true')
    parser.add_argument('--venv-dir',default='.venv',help='Local isolated Python environment; never packaged.')
    args=parser.parse_args(argv)
    try:
        result=check()
        if args.check:
            print(json.dumps(result));return
        envdir=Path(args.venv_dir).expanduser().resolve()
        executable=envdir/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not executable.is_file():
            if envdir.exists() and any(envdir.iterdir()):raise ValueError('The selected environment folder is not empty and has no Python; choose another folder.')
            venv.EnvBuilder(with_pip=False).create(envdir)
        probe=subprocess.run([str(executable),'-c','import sys,sqlite3;raise SystemExit(sys.version_info<(3,10))'],capture_output=True)
        if probe.returncode:raise ValueError('The existing environment cannot run Python 3.10+/SQLite. Choose another environment folder.')
        print('Local setup ready. No third-party runtime packages or network downloads needed.',flush=True)
        if args.start:
            command=[str(executable),'-m','cashflow_copilot.local_app','--db',args.db,'--port',str(args.port),'--example',args.example]
            if args.telegram:command.append('--telegram')
            if args.open_browser:command.append('--open-browser')
            raise SystemExit(subprocess.call(command,cwd=ROOT))
    except (OSError,ValueError,AssertionError):
        parser.exit(1,'Local setup could not complete. Check Python 3.10+, complete extraction, writable folders and the selected environment path. No existing database was reset.\n')
    except KeyboardInterrupt:parser.exit(130,'Local app stopped. Saved data is retained.\n')


if __name__=='__main__':main()
