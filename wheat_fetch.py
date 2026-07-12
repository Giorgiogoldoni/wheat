#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAPTOR Wheat — Data Fetch
Scarica dati CBOT Wheat Futures (25 anni) e 3WHL.MI (dalla nascita)
Calcola: stagionalità, momentum Antonacci, indicatori RAPTOR, livelli supporto

Schedule:
- 05:30 CET: Analisi completa notturna + aggiornamento storico
- 16:45 CET: Rilevazione intra-day (segnali aggiornati)
- 17:00 CET: Chiusura giornaliera + salvataggio completo
"""

import json, math, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import yfinance as yf

# ── RILEVAMENTO ORARIO ─────────────────────────────────
def get_execution_type():
    """Determina il tipo di esecuzione basato sull'orario UTC"""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    minute = now_utc.minute
    
    # 05:30 CET = 04:30 UTC
    if 4 <= hour < 5 or (hour == 4 and minute >= 30):
        return 'morning'
    # 16:45 CET = 15:45 UTC
    elif 15 <= hour < 16 or (hour == 15 and minute >= 45):
        return 'intraday'
    # 17:00 CET = 16:00 UTC
    elif 16 <= hour < 17 or (hour == 16 and minute >= 0):
        return 'close'
    else:
        return 'manual'

# ── INDICATORI ────────────────────────────────────────
def calc_kama(closes, n=10, fast=2, slow=30):
    fsc = 2/(fast+1); ssc = 2/(slow+1)
    kama = [None]*len(closes)
    if len(closes) <= n: return kama
    kama[n] = closes[n]
    for i in range(n+1, len(closes)):
        d = abs(closes[i]-closes[i-n])
        v = sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1, i+1))
        er = d/v if v else 0
        sc = (er*(fsc-ssc)+ssc)**2
        kama[i] = kama[i-1] + sc*(closes[i]-kama[i-1])
    return kama

def calc_rsi(closes, n=14):
    res = [None]*len(closes)
    for i in range(n+1, len(closes)):
        gs=[]; ls=[]
        for j in range(i-n, i+1):
            dd = closes[j]-closes[j-1]
            gs.append(max(dd,0)); ls.append(max(-dd,0))
        ag=sum(gs)/n; al=sum(ls)/n
        res[i] = round(100-100/(1+ag/al),2) if al>0 else 100.0
    return res

def calc_ao(highs, lows):
    mid = [(h+l)/2 for h,l in zip(highs,lows)]
    def ema(arr, p):
        k=2/(p+1); e=arr[0]; out=[e]
        for x in arr[1:]: e=x*k+e*(1-k); out.append(e)
        return out
    if len(mid)<13: return [0]*len(mid)
    e3=ema(mid,3); e13=ema(mid,13)
    return [round(a-b,4) for a,b in zip(e3,e13)]

def calc_sar(high, low, step=0.03, max_af=0.25):
    n=len(high); sar=[None]*n
    if n<5: return sar
    bull=high[1]>high[0]; af=step
    ep=max(high[:2]) if bull else min(low[:2])
    sar[1]=min(low[:2]) if bull else max(high[:2])
    for i in range(2,n):
        ps=sar[i-1]
        if bull:
            sar[i]=min(ps+af*(ep-ps), low[i-1], low[i-2] if i>=2 else low[i-1])
            if low[i]<sar[i]: bull=False; af=step; sar[i]=ep; ep=low[i]
            else:
                if high[i]>ep: ep=high[i]; af=min(af+step,max_af)
        else:
            sar[i]=max(ps+af*(ep-ps), high[i-1], high[i-2] if i>=2 else high[i-1])
            if high[i]>sar[i]: bull=True; af=step; sar[i]=ep; ep=high[i]
            else:
                if low[i]<ep: ep=low[i]; af=min(af+step,max_af)
    return sar

def calc_er(closes, n=10):
    res=[0]*len(closes)
    for i in range(n,len(closes)):
        d=abs(closes[i]-closes[i-n])
        v=sum(abs(closes[j]-closes[j-1]) for j in range(i-n+1,i+1))
        res[i]=round(d/v,4) if v else 0
    return res

def sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj): return None
        return obj
    if isinstance(obj, dict): return {k:sanitize(v) for k,v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    return obj

# ── STAGIONALITÀ 25 ANNI ──────────────────────────────
def calc_stagionalita(closes, dates):
    """Rendimento medio mensile su 25 anni"""
    monthly_rets = defaultdict(list)
    for i in range(1, len(closes)):
        if closes[i] and closes[i-1]:
            month = int(dates[i][5:7])
            ret = (closes[i]-closes[i-1])/closes[i-1]*100
            monthly_rets[month].append(ret)

    # Rendimento cumulativo mensile medio
    stagionalita = []
    mesi = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic']
    for m in range(1,13):
        rets = monthly_rets[m]
        avg = sum(rets)/len(rets) if rets else 0
        positive = sum(1 for r in rets if r>0)
        wr = positive/len(rets)*100 if rets else 0
        stagionalita.append({
            'mese': m,
            'nome': mesi[m-1],
            'avg_ret': round(avg,3),
            'win_rate': round(wr,1),
            'n_anni': len(rets)
        })
    return stagionalita

# ── MOMENTUM ANTONACCI ────────────────────────────────
def calc_antonacci(closes, dates, lookback_months=12):
    """
    Dual Momentum assoluto: se il rendimento a 12 mesi > 0 → BUY, altrimenti → OUT
    """
    results = []
    approx_days = lookback_months * 21  # ~giorni di trading
    for i in range(approx_days, len(closes)):
        if closes[i] and closes[i-approx_days]:
            ret_12m = (closes[i]-closes[i-approx_days])/closes[i-approx_days]*100
            signal = 'BUY' if ret_12m > 0 else 'OUT'
            results.append({
                'date': dates[i],
                'price': closes[i],
                'ret_12m': round(ret_12m,2),
                'signal': signal
            })
    return results

# ── SUPPORTI ─────────────────────────────────────────
def find_supports(closes, dates, window=3):
    supports = []
    for i in range(window, len(closes)-window):
        if not closes[i]: continue
        is_min = all(closes[i] <= closes[i-j] for j in range(1,window+1) if closes[i-j]) and \
                 all(closes[i] <= closes[i+j] for j in range(1,window+1) if closes[i+j])
        if is_min:
            supports.append({'date': dates[i], 'price': closes[i]})
    return supports[-20:]  # ultimi 20 supporti

# ── MAIN ─────────────────────────────────────────────
def main():
    now = datetime.now()
    exec_type = get_execution_type()
    print(f"RAPTOR Wheat Fetch — {now.strftime('%Y-%m-%d %H:%M')} [{exec_type.upper()}]")

    # ── CBOT Wheat Futures (25 anni) ──────────────────
    print("Scarico CBOT Wheat Futures (ZW=F)...")
    cbot = yf.download("ZW=F", start="2000-01-01", interval="1d",
                       auto_adjust=True, progress=False)

    # Appiattisci MultiIndex se presente (yfinance recente)
    if hasattr(cbot.columns, 'levels'):
        cbot.columns = cbot.columns.get_level_values(0)
    cbot_closes = [round(float(c),4) for c in cbot['Close'].tolist()]
    cbot_highs  = [round(float(c),4) for c in cbot['High'].tolist()]
    cbot_lows   = [round(float(c),4) for c in cbot['Low'].tolist()]
    cbot_dates  = [ts.strftime('%Y-%m-%d') for ts in cbot.index]
    print(f"CBOT: {len(cbot_closes)} barre ({cbot_dates[0]} → {cbot_dates[-1]})")

    # ── 3WHL.MI (dalla nascita) ───────────────────────
    print("Scarico 3WHL.MI...")
    whl = yf.download("3WHL.MI", start="2018-01-01", interval="1d",
                      auto_adjust=True, progress=False)

    if hasattr(whl.columns, 'levels'):
        whl.columns = whl.columns.get_level_values(0)
    whl_closes  = [round(float(c),4) for c in whl['Close'].tolist()]
    whl_highs   = [round(float(c),4) for c in whl['High'].tolist()]
    whl_lows    = [round(float(c),4) for c in whl['Low'].tolist()]
    whl_volumes = [int(v) for v in whl['Volume'].tolist()]
    whl_dates   = [ts.strftime('%Y-%m-%d') for ts in whl.index]
    print(f"3WHL: {len(whl_closes)} barre ({whl_dates[0]} → {whl_dates[-1]})")

    # ── ANALISI COMPLETA (MORNING + CLOSE) ─────────────
    if exec_type in ('morning', 'close', 'manual'):
        print(f"[{exec_type.upper()}] Calcolo analisi completa...")
        
        # KAMA su CBOT
        cbot_kama_fast = calc_kama(cbot_closes, n=5,  fast=3, slow=20)
        cbot_kama_slow = calc_kama(cbot_closes, n=20, fast=2, slow=40)

        # Stagionalità 25 anni
        stagionalita = calc_stagionalita(cbot_closes, cbot_dates)

        # Momentum Antonacci
        antonacci_full = calc_antonacci(cbot_closes, cbot_dates)
        antonacci_latest = antonacci_full[-1] if antonacci_full else {}

        # Supporti CBOT
        cbot_supports = find_supports(cbot_closes, cbot_dates)

        # Indicatori RAPTOR su 3WHL
        whl_kama_fast = calc_kama(whl_closes, n=5,  fast=3, slow=20)
        whl_kama_slow = calc_kama(whl_closes, n=20, fast=2, slow=40)
        whl_rsi14     = calc_rsi(whl_closes, 14)
        whl_rsi5      = calc_rsi(whl_closes, 5)
        whl_ao        = calc_ao(whl_highs, whl_lows)
        whl_sar       = calc_sar(whl_highs, whl_lows)
        whl_er        = calc_er(whl_closes, 10)

        # Segnali RAPTOR
        whl_signals = []
        avg_vol = sum(whl_volumes[-21:-1])/20 if len(whl_volumes)>21 else 1
        for i in range(25, len(whl_closes)):
            kf=whl_kama_fast[i]; ks=whl_kama_slow[i]
            if kf is None or ks is None:
                whl_signals.append(None); continue
            p=whl_closes[i]
            if p>kf and kf>ks:   zona='LONG_CONF'
            elif p>kf and p>ks:  zona='LONG_EARLY'
            elif p<ks:           zona='STOP' if (ks-p)/ks*100>2 else 'USCITA'
            else:                zona='GRIGIA'
            vr=whl_volumes[i]/avg_vol if avg_vol>0 else 1
            gap_ok=ks>0 and abs(kf-ks)/ks>=0.003
            ao=whl_ao[i] if i<len(whl_ao) else 0
            sig=None
            # Baff
            baff=0
            for j in range(max(0,i-5),i+1):
                if whl_kama_fast[j] and whl_closes[j]>whl_kama_fast[j]: baff+=1
                else: baff=0
            if zona=='LONG_CONF' and ao>0 and vr>=2 and baff>=3 and whl_er[i]>=0.35 and gap_ok:
                sig='BUY3'
            elif zona=='LONG_EARLY' and ao>0 and vr>=1.5 and baff>=2 and whl_er[i]>=0.35:
                sig='BUY2'
            elif zona in ('STOP','USCITA'): sig='SELL'
            whl_signals.append(sig)
        whl_signals = [None]*25 + whl_signals  # fix: allinea signals a closes (era disallineato di 25 barre)

        # Antonacci su 3WHL
        whl_antonacci = calc_antonacci(whl_closes, whl_dates)
        whl_antonacci_latest = whl_antonacci[-1] if whl_antonacci else {}

        # Supporti 3WHL
        whl_supports = find_supports(whl_closes, whl_dates)

        # Simulazione mediazione
        carico = 0.23
        qty = 50000
        prezzo_now = whl_closes[-1]
        livelli_med = []
        for lv in [0.110, 0.100, 0.095, 0.090]:
            for qty_add in [25000, 50000, 100000]:
                nuovo_carico = (carico*qty + lv*qty_add)/(qty+qty_add)
                livelli_med.append({
                    'livello': lv,
                    'qty_aggiunta': qty_add,
                    'nuovo_carico': round(nuovo_carico,4),
                    'tot_investito': round(carico*qty + lv*qty_add, 0),
                    'qty_totale': qty+qty_add
                })

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        output = sanitize({
            'execution_type': exec_type,
            'updated_at': now.isoformat(),
            'updated_display': now.strftime('%d/%m/%Y %H:%M'),

            # CBOT (ultimi 3 anni per il grafico principale)
            'cbot': {
                'dates':     cbot_dates[-756:],
                'closes':    cbot_closes[-756:],
                'highs':     cbot_highs[-756:],
                'lows':      cbot_lows[-756:],
                'kama_fast': fmt(cbot_kama_fast[-756:]),
                'kama_slow': fmt(cbot_kama_slow[-756:]),
            },

            # Stagionalità (25 anni)
            'stagionalita': stagionalita,

            # Momentum Antonacci su CBOT
            'antonacci_cbot': antonacci_full[-252:],  # ultimo anno
            'antonacci_latest': antonacci_latest,

            # 3WHL completo
            'whl': {
                'dates':     whl_dates,
                'closes':    whl_closes,
                'highs':     whl_highs,
                'lows':      whl_lows,
                'volumes':   whl_volumes,
                'kama_fast': fmt(whl_kama_fast),
                'kama_slow': fmt(whl_kama_slow),
                'rsi14':     fmt(whl_rsi14),
                'rsi5':      fmt(whl_rsi5),
                'ao':        fmt(whl_ao),
                'sar':       fmt(whl_sar),
                'er':        whl_er,
                'signals':   whl_signals,
            },

            # Antonacci su 3WHL
            'antonacci_whl': whl_antonacci[-252:],
            'antonacci_whl_latest': whl_antonacci_latest,

            # Supporti
            'cbot_supports': cbot_supports,
            'whl_supports':  whl_supports,

            # Simulazione mediazione
            'mediazione': {
                'carico_attuale': carico,
                'qty_attuale': qty,
                'prezzo_now': prezzo_now,
                'pl_pct': round((prezzo_now-carico)/carico*100,2),
                'livelli': livelli_med
            }
        })

    # ── ANALISI LEGGERA INTRADAY (16:45) ────────────────
    else:  # intraday
        print(f"[INTRADAY] Calcolo segnali veloci...")
        
        # Carica il JSON precedente per mantenere storico
        try:
            with open('wheat.json','r',encoding='utf-8') as f:
                output = json.load(f)
        except:
            output = {}

        # Aggiorna solo gli indicatori attuali
        whl_kama_fast = calc_kama(whl_closes, n=5,  fast=3, slow=20)
        whl_kama_slow = calc_kama(whl_closes, n=20, fast=2, slow=40)
        whl_rsi14     = calc_rsi(whl_closes, 14)
        whl_rsi5      = calc_rsi(whl_closes, 5)
        whl_ao        = calc_ao(whl_highs, whl_lows)
        whl_sar       = calc_sar(whl_highs, whl_lows)
        whl_er        = calc_er(whl_closes, 10)

        # Ricalcola i segnali RAPTOR (ricalcolo completo per garantire l'allineamento)
        def calc_signals(closes, kama_fast, kama_slow, volumes, ao_arr, er_arr):
            signals = []
            avg_vol = sum(volumes[-21:-1])/20 if len(volumes)>21 else 1
            for i in range(25, len(closes)):
                kf=kama_fast[i]; ks=kama_slow[i]
                if kf is None or ks is None:
                    signals.append(None); continue
                p=closes[i]
                if p>kf and kf>ks:   zona='LONG_CONF'
                elif p>kf and p>ks:  zona='LONG_EARLY'
                elif p<ks:           zona='STOP' if (ks-p)/ks*100>2 else 'USCITA'
                else:                zona='GRIGIA'
                vr=volumes[i]/avg_vol if avg_vol>0 else 1
                gap_ok=ks>0 and abs(kf-ks)/ks>=0.003
                ao=ao_arr[i] if i<len(ao_arr) else 0
                sig=None
                baff=0
                for j in range(max(0,i-5),i+1):
                    if kama_fast[j] and closes[j]>kama_fast[j]: baff+=1
                    else: baff=0
                if zona=='LONG_CONF' and ao>0 and vr>=2 and baff>=3 and er_arr[i]>=0.35 and gap_ok:
                    sig='BUY3'
                elif zona=='LONG_EARLY' and ao>0 and vr>=1.5 and baff>=2 and er_arr[i]>=0.35:
                    sig='BUY2'
                elif zona in ('STOP','USCITA'): sig='SELL'
                signals.append(sig)
            return [None]*25 + signals

        whl_signals = calc_signals(whl_closes, whl_kama_fast, whl_kama_slow, whl_volumes, whl_ao, whl_er)

        def fmt(arr):
            return [round(v,4) if v is not None else None for v in arr]

        # Aggiorna il JSON con nuovi indicatori
        output['execution_type'] = exec_type
        output['updated_at'] = now.isoformat()
        output['updated_display'] = now.strftime('%d/%m/%Y %H:%M')
        output['whl']['dates'] = whl_dates
        output['whl']['closes'] = whl_closes
        output['whl']['highs'] = whl_highs
        output['whl']['lows'] = whl_lows
        output['whl']['volumes'] = whl_volumes
        output['whl']['kama_fast'] = fmt(whl_kama_fast)
        output['whl']['kama_slow'] = fmt(whl_kama_slow)
        output['whl']['rsi14'] = fmt(whl_rsi14)
        output['whl']['rsi5'] = fmt(whl_rsi5)
        output['whl']['ao'] = fmt(whl_ao)
        output['whl']['sar'] = fmt(whl_sar)
        output['whl']['er'] = whl_er
        output['whl']['signals'] = whl_signals
        
        output = sanitize(output)

    os.makedirs('data', exist_ok=True)
    with open('wheat.json','w',encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',',':'), allow_nan=False)
    print(f"✅ wheat.json aggiornato [{exec_type}]")

if __name__ == '__main__':
    main()
