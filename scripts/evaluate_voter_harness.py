"""Repeat end-to-end conversations against fictional data with reference outcomes.

--live authorizes billed model calls. No real constituency records are read.
--source-root can point at an immutable previous deployment snapshot for baseline.
"""
import argparse
from contextlib import closing, nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', action='store_true')
    p.add_argument('--gcloud-auth', action='store_true')
    p.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--trials', type=int, default=5)
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--scenario', action='append')
    args = p.parse_args()
    if not args.live or not 1 <= args.trials <= 10 or not 1 <= args.workers <= 3:
        p.error('Use --live, 1-10 trials, and 1-3 workers')
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / 'backend'), str(root)]
    from agents import local_runner as runner
    from app.config import get_settings
    authentication = nullcontext()
    if args.gcloud_auth:
        from google.oauth2.credentials import Credentials
        token = subprocess.run(['gcloud', 'auth', 'print-access-token'], capture_output=True, text=True, check=True).stdout.strip()
        authentication = patch('google.auth.default', return_value=(Credentials(token), 'my-booth-agent'))
    scenarios = {
        'caste_category': [('How many Yadav caste voters?', 2), ('How many OBC voters?', 4), ('How many Nishad caste voters?', 1)],
        'caste_clarification': [('How many ishad?', None), ('nishad', 1)],
        'population': [('How many voters are there?', 5), ('How many of these are Muslims?', 3),
                       ('How many women among them?', 2), ('Total voters in the constituency', 5)],
        'demographic_hindi': [('यहाँ कितने मुस्लिम मतदाता हैं?', 3)],
        'combined': [('How many Muslim women are over 60?', 1)],
        'alternatives': [('How many female voters are recorded as either Muslim or Hindu?', 3)],
        'booth_count': [('How many Muslim voters in booth 1?', 2)],
        'unknown_category': [('How many voters have religion Category Z?', None)],
        'house_number': [('Find लीला in booth 1', 'TEST3'), ('What is her house number?', '2')],
        'election': [('Who won the 2022 Assembly election?', 'Candidate Alpha'),
                     ('In 2024', 'Candidate Alpha')],
    }
    if args.scenario and any(s not in scenarios for s in args.scenario):
        p.error('Unknown scenario')
    with tempfile.TemporaryDirectory() as directory, authentication:
        seat = Path(directory) / 'synthetic-seat'
        (seat / 'booth_analysis').mkdir(parents=True)
        with closing(sqlite3.connect(seat / 'voters.db')) as conn, conn:
            conn.execute('CREATE TABLE voters(name TEXT,relative_name TEXT,house_no TEXT,age INTEGER,gender TEXT,religion TEXT,caste TEXT,epic_id TEXT,polling_station TEXT,region TEXT,relation_type TEXT,social_category TEXT)')
            conn.executemany('INSERT INTO voters VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', [
                ('श्याम','राम','1',65,'Female','Muslim','यादव','TEST1','School','East','Husband','OBC'),
                ('श्याम','राम','1',40,'Male','Hindu','यादव','TEST2','School','East','Father','OBC'),
                ('लीला','राम','2',70,'Male','Muslim','निषाद','TEST3','School','East','Father','OBC'),
                ('राधा','राम','3',25,'Female','Hindu','ब्राह्मण','TEST4','College','West','Father','General'),
                ('आरती','राम','4',35,'Female','Muslim','कुर्मी','TEST5','College','West','Father','OBC')])
        with closing(sqlite3.connect(seat / 'history.db')) as conn, conn:
            for year, kind in [(2017, 'vidhan'), (2019, 'lok'), (2022, 'vidhan'), (2024, 'lok')]:
                table = f'form20_{year}_{kind}'
                conn.execute(f'CREATE TABLE {table}(s_no INTEGER,polling_station TEXT,area TEXT,candidate TEXT,party TEXT,votes INTEGER)')
                if year != 2017:
                    conn.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?)', [(1, 'School', 'East', 'Candidate Alpha', 'Test Party A', 100), (1, 'School', 'East', 'Candidate Beta', 'Test Party B', 60)])
        (seat / 'boothlist.csv').write_text('part_no,pooling_station,region,total_voters\n1,School,East,3\n2,College,West,2\n')
        (seat / 'booth_analysis/part1.json').write_text(json.dumps({'booth_metadata': {'booth_name': 'Part_1', 'total_registered_voters': 3}, 'gender_demographics': {'Female': 1, 'Male': 2}}))
        get_settings().local_data_root = directory
        jobs = [(name, index + 1) for name in scenarios if not args.scenario or name in args.scenario for index in range(args.trials)]
        def run(job):
            name, index = job
            history, context, pending, turns = [], {}, None, []
            for question, expected in scenarios[name]:
                result = runner.run_local_agent(question, 'synthetic-seat', history=history, voter_context=context, pending_state=pending)
                answer = result['answer']
                passed = result['status'] in {'complete', 'partial'}
                if isinstance(expected, int):
                    numbers = [int(s.replace(',', '')) for s in re.findall(r'(?<![\w.])\d[\d,]*(?![\w.])', answer)]
                    passed &= expected in numbers
                elif expected is None:
                    passed = result['status'] == 'needs_clarification' and not re.search(r'\b0\s+voters|\bzero\b', answer, re.I)
                else:
                    passed &= expected in answer
                if name.startswith('caste_') and expected is not None:
                    field, label = ('social_category', 'OBC') if 'OBC' in question else ('caste', 'यादव' if 'Yadav' in question else 'निषाद')
                    scopes = [scope for event in result.get('turn_trace', {}).get('events', []) for scope in event.get('resolved_scopes', [])]
                    passed &= any(scope.get('total') == expected and any(f['field'] == field and label in f.get('resolved_values', []) for f in scope.get('filters', [])) for scope in scopes)
                if name == 'election' and question == 'In 2024':
                    passed &= '2024' in answer and 'segment' in answer.lower() and 'not taken place' not in answer.lower()
                turns.append({'question': question, 'answer': answer, 'status': result['status'], 'passed': bool(passed), 'agents': result['agent_path'], 'diagnostics': result.get('diagnostics', [])})
                context, pending = result.get('voter_context', {}), result.get('pending_state')
                history.extend([{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}])
            return {'scenario': name, 'trial': index, 'passed': all(t['passed'] for t in turns), 'turns': turns}
        outcomes = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for outcome in pool.map(run, jobs):
                outcomes.append(outcome)
                print(json.dumps(outcome, ensure_ascii=False), flush=True)
        summary = {'evaluation': 'synthetic_end_to_end', 'source_root': str(root), 'passed': sum(r['passed'] for r in outcomes), 'total': len(outcomes)}
        print(json.dumps(summary), flush=True)
        return 0 if all(r['passed'] for r in outcomes) else 1


if __name__ == '__main__':
    raise SystemExit(main())
