"""Live, cache-free source audit. Each call is isolated and has a hard timeout.

Run: .venv/bin/python tests/audit_market_sources.py
Raw public results and timings: evaluation/reports/source_audit_20260905/.
A successful response is not a claim of semantic correctness or long-term SLA.
"""
from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'evaluation/reports/source_audit_20260905'

async def probe(kind, symbol):
    import httpx
    from toolkit.market.sources.market_providers import (
        EMQuoteProvider, SinaQuoteProvider, AKShareProvider, WeStockProvider,
    )
    code, ex = symbol.split('.')
    if kind in ('eastmoney', 'sina'):
        async with httpx.AsyncClient(timeout=10) as client:
            cls = EMQuoteProvider if kind == 'eastmoney' else SinaQuoteProvider
            result = await cls(client).snapshot(symbol)
    elif kind == 'akshare':
        result = AKShareProvider()._fundamentals_sync(code, ex, 3)
    elif kind == 'westock':
        provider = WeStockProvider(timeout=12, years=3)
        wc = ex.lower() + code
        result = {'snapshot': asdict(await provider.snapshot(wc)),
                  'fundamentals': asdict(await provider.fundamentals(wc))}
    elif kind == 'westock_raw':
        provider = WeStockProvider(timeout=12, years=8)
        result = {}
        for typ in ('sum','lrb','zcfz','xjll'):
            result[typ] = await provider._run('finance', ex.lower()+code, '--type',typ,'--num','8')
        result['profile'] = await provider._run('profile', ex.lower()+code)
        result['kline'] = await provider._run('kline', ex.lower()+code,'--period','day','--limit','2')
    elif kind in ('em_indicator','sina_income','sina_balance','sina_cashflow','sina_abstract','xq','em_income','em_balance','em_cashflow','em_info'):
        import akshare as ak
        funcs = {
            'em_indicator': (ak.stock_financial_analysis_indicator_em, {'symbol':symbol}),
            'sina_income': (ak.stock_financial_report_sina, {'stock':ex.lower()+code,'symbol':'利润表'}),
            'sina_balance': (ak.stock_financial_report_sina, {'stock':ex.lower()+code,'symbol':'资产负债表'}),
            'sina_cashflow': (ak.stock_financial_report_sina, {'stock':ex.lower()+code,'symbol':'现金流量表'}),
            'sina_abstract': (ak.stock_financial_abstract, {'symbol':code}),
            'xq': (ak.stock_individual_spot_xq, {'symbol':ex+code,'timeout':10}),
            'em_income': (ak.stock_profit_sheet_by_report_em, {'symbol':ex+code}),
            'em_balance': (ak.stock_balance_sheet_by_report_em, {'symbol':ex+code}),
            'em_cashflow': (ak.stock_cash_flow_sheet_by_report_em, {'symbol':ex+code}),
            'em_info': (ak.stock_individual_info_em, {'symbol':code,'timeout':10}),
        }
        fn, params = funcs[kind]
        df = fn(**params)
        # Keep all columns and recent periods, enough to match exact labels and dates.
        result = {'columns':list(df.columns),'rows':json.loads(df.head(100 if kind=='sina_abstract' else 12).to_json(orient='records',force_ascii=False)), 'row_count':len(df)}
    elif kind in ('em_valuation','em_quote_alt','tencent_quote'):
        async with httpx.AsyncClient(timeout=10) as client:
            if kind == 'em_valuation':
                r = await client.get('https://datacenter-web.eastmoney.com/api/data/v1/get', params={
                    'reportName':'RPT_VALUEANALYSIS_DET','columns':'ALL','filter':f'(SECURITY_CODE="{code}")',
                    'sortColumns':'TRADE_DATE','sortTypes':'-1','pageSize':'2','pageNumber':'1'})
            elif kind == 'em_quote_alt':
                r = await client.get('https://push2delay.eastmoney.com/api/qt/stock/get', params={
                    'secid':('1.' if ex=='SH' else '0.')+code,'fields':EMQuoteProvider._FIELDS,'fltt':'2'})
            else:
                r = await client.get('https://qt.gtimg.cn/q='+ex.lower()+code)
            r.raise_for_status()
            result = r.content.decode('gbk') if kind=='tencent_quote' else r.json()
    else:
        raise ValueError(kind)
    return asdict(result) if is_dataclass(result) else result

def lane(kind):
    for trial in range(1,4):
        for symbol in ('600519.SH','000001.SZ','300750.SZ'):
            started = time.monotonic()
            record = dict(kind=kind, symbol=symbol, trial=trial,
                          checked_at=datetime.now(timezone.utc).isoformat())
            try:
                r = subprocess.run([sys.executable, __file__, '--worker', kind, symbol],
                                   capture_output=True, text=True, timeout=65)
                record['result'] = json.loads(r.stdout) if r.returncode == 0 else None
                record['error'] = r.stderr[-1500:] if r.returncode else ''
            except subprocess.TimeoutExpired:
                record.update(result=None, error='hard timeout after 65s')
            except Exception as e:
                record.update(result=None, error=f'{type(e).__name__}: {e}')
            record['seconds'] = round(time.monotonic()-started,3)
            (OUT / f'{kind}_{symbol}_{trial}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str))
            print(kind,symbol,trial,record['seconds'],'ok' if record.get('result') else 'FAILED',flush=True)

def summarize():
    from toolkit.market.source_mapping import SOURCE_MAPPING, UNMAPPED_FIELDS, AUDIT_RESULTS
    from toolkit.market.sources.market_providers import WeStockProvider
    OUT=Path('evaluation/reports/source_audit_20260905')

    def records(spec, record):
        r=record.get('result')
        if not r:return []
        kind=spec.audit_key
        if kind=='tencent_quote':
            cells=r.split('"')[1].split('~')
            return [(cells[30][:8],cells[int(spec.source_field)])]
        if kind=='sina':
            return [('latest',r.get({'0':'name','3':'price'}[spec.source_field]))]
        if kind=='em_quote_alt':return [('latest',(r.get('data') or {}).get(spec.source_field))]
        if kind=='em_valuation':return [(x['TRADE_DATE'][:10].replace('-',''),x.get(spec.source_field)) for x in (r.get('result') or {}).get('data',[])]
        if kind=='westock_raw':
            table=spec.request.split('--type ')[1].split()[0]
            _, rows=WeStockProvider._parse_md(r.get(table,''))
            return [(x.get('_date','').replace('-',''),x.get(spec.source_field)) for x in rows]
        if kind=='sina_abstract':
            return [(k,v) for row in r['rows'] if row['指标']==spec.source_field for k,v in row.items() if re.fullmatch(r'\d{8}',k)]
        return [(str(row.get('REPORT_DATE',row.get('报告日','')))[:10].replace('-',''),row.get(spec.source_field)) for row in r['rows']]

    def value(v,spec):
        if v is None or str(v).strip() in ('','-','--','nan','None'):return None
        try:return float(v)*spec.scale
        except (ValueError,TypeError):return v

    rows=[]
    for field,specs in SOURCE_MAPPING.items():
        for spec in specs:
            attempts=[]
            if spec.audit_key=='preset':
                local=json.loads(Path('toolkit/entity/data/known_a_share.json').read_text())
                for code in ['600519','000001','300750']:
                    hits=[r[spec.source_field] for r in local if r['code']==code];assert len(hits)==1
                    attempts.append({'symbol':code,'trial':1,'non_null':True,'samples':{'latest':hits[0]}})
            else:
                for file in sorted(OUT.glob(f'{spec.audit_key}_*.json')):
                    record=json.loads(file.read_text())
                    if record.get('kind') != spec.audit_key:
                        continue
                    vals={day:value(v,spec) for day,v in records(spec,record) if value(v,spec) is not None}
                    attempts.append({'symbol':record['symbol'],'trial':record['trial'],'non_null':bool(vals),
                                     'samples':{k:v for k,v in vals.items() if k in ('20251231','20260630','20260904','latest')},
                                     'latest_returned_period':max(vals,default=None)})
            rows.append({'field':field,**spec.to_dict(),'value_hits':sum(x['non_null'] for x in attempts),'attempts':len(attempts),'observations':attempts})
    result={'checked_on':'2026-09-05','scope':'A股三只样本各三轮；无业务缓存；短时可重复性，不是长期稳定性保证',
            'endpoints':AUDIT_RESULTS,'routes':rows,'unmapped':UNMAPPED_FIELDS}
    (OUT/'field_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))

    # Compact checked-in receipts make the field/column contract reproducible offline.
    compact = {}
    for r in rows:
        key = '|'.join((r['field'], r['audit_key'], r['source_field']))
        compact[key] = {'request':r['request'], 'hits':r['value_hits'], 'attempts':r['attempts'],
                        'samples':{o['symbol']:o['samples'] for o in r['observations'] if o['trial']==1}}
    fixture = ROOT / 'tests/data/source_mapping_audit_20260905.json'
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps(compact,ensure_ascii=False,indent=2))
    print('Audit summarized:', len(rows), 'routes;', sum(not r['value_hits'] for r in rows), 'without values')


if __name__ == '__main__':
    if sys.argv[1:] == ['--summarize']:
        summarize()
    elif len(sys.argv)>1 and sys.argv[1]=='--worker':
        print(json.dumps(asyncio.run(probe(*sys.argv[2:])),ensure_ascii=False,default=str))
    else:
        OUT.mkdir(parents=True,exist_ok=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lane, tuple(sys.argv[1:]) or ('eastmoney','sina','akshare','westock')))
