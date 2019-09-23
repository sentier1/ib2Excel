from datetime import datetime as dt

import numpy as np
import pandas as pd
import xlwings as xw
from ib_insync import *
import sys, signal
from setup import *


def stream_data():
    def signal_handler(signal, frame):
        print("closing....")
        ib.disconnect()
        sys.exit(0)

    def get_libor_func():
        print('retrieving Libor rates....')
        tables = pd.read_html("https://www.finanzen.net/zinsen/libor/usd")
        df_lib = tables[0]
        df_lib["Kurs"] = pd.to_numeric(df_lib["Kurs"]).div(1000000)
        df_lib.reset_index(inplace=True, drop=True)
        df_lib['Years'] = pd.Series([1 / 365, 7 / 365, 30 / 365, 60 / 365, 90 / 365, 180 / 365, 1],
                                    index=range(7)).round(3)
        # rfr_func = interp1d(df_lib['Years'], df_lib["Kurs"], fill_value="extrapolate" )

        # format df to fit export format
        df_lib = df_lib[['Name', 'Kurs']]
        df_lib = df_lib.pivot_table(columns='Name', index=None, values='Kurs', aggfunc='first') * 100

        cols = ["Date", "Libor USD Overnight", "Libor USD 1 Woche", "Libor USD 1 Monat", "Libor USD 2 Monate",
                "Libor USD 3 Monate", "Libor USD 6 Monate", "Libor USD 12 Monate"]

        df_lib['Date'] = dt.today().date().strftime('%d.%m.%y')
        df_lib = df_lib[cols]
        return df_lib

    def conn_tws(client=np.random.randint(0, 10)):
        i = 0
        while ib.isConnected() == False and i < 20:
            try:
                print("connecting...")
                ib.connect('127.0.0.1', tws_port, clientId=client, timeout=None)  # tws 7496 gateway 4001 (see TWS settings)
                ib.sleep(1)
            except ConnectionRefusedError:
                ib.sleep(3)
                i += 1
        if ib.isConnected():
            print("Connection established")
        else:
            print("Timeout")

    def get_tickers(min_dte=30, max_dte=250, strike_distance=25, strike_range=0.5):
        ib.reqMarketDataType(marketDataType=2)
        print('getting SPX...')
        spx = Index('SPX', 'CBOE')
        ib.qualifyContracts(spx)
        print('getting chains...')
        chains = ib.reqSecDefOptParams(spx.symbol, '', spx.secType, spx.conId)
        chain_spx = next(c for c in chains if (c.tradingClass == 'SPX' and c.exchange == 'SMART'))
        chain_spxw = next(c for c in chains if (c.tradingClass == 'SPXW' and c.exchange == 'SMART'))

        print('getting SPX live...')
        spx_ticker = ib.reqMktData(spx, '', False, False)
        ib.sleep(1)
        spxValue = spx_ticker.marketPrice()
        print(spxValue)
        ib.sleep(1)

        print('combining SPX/SPXW expirations...')
        strikes = [strike for strike in chain_spx.strikes
                   if strike % strike_distance == 0
                   and spxValue - strike_range * spxValue < strike < spxValue + strike_range * spxValue]

        expirations_spx = sorted(exp for exp in chain_spx.expirations)
        # print(expirations_spx)
        expirations_spxw = sorted(exp for exp in chain_spxw.expirations)
        # print (expirations_spxw)

        expirations = expirations_spx + expirations_spxw
        print(expirations)

        print('filtering for min and max dte...')

        dt_dates = [dt.strptime(date, '%Y%m%d') for date in expirations]
        dt_dates = [date for date in dt_dates
                    if max_dte > (date - dt.today()).days > min_dte]
        expirations = [d.strftime('%Y%m%d') for d in dt_dates]
        print(expirations)

        # alle Chain Contracts
        print('finding contracts...')
        contracts = [Option('SPX', exp, strike, right, 'SMART')
                     for strike in strikes
                     # for tradingClass in ['SPX','SPXW']
                     for right in ['P', 'C']
                     for exp in expirations
                     ]

        startTime = dt.now()

        ib.qualifyContracts(*contracts)
        contracts = [c for c in contracts if c.conId]
        contracts = list(set(contracts))
        ib.qualifyContracts(*contracts)
        print('number of contracts: ' + str(len(contracts)))
        setupTime = dt.now() - startTime
        print('finished finding contracts in ' + str(setupTime) + 's')

        print('setting up ticker...')
        startTime = dt.now()
        tickers = ib.reqTickers(*contracts)
        setupTime = dt.now() - startTime
        print('finished setting up ticker in ' + str(setupTime) + 's')
        print('Startup finished!')
        return tickers, spx_ticker, contracts

    def update_price(tickers, spx_ticker, contracts):

        now = dt.now()
        spxValue = spx_ticker.marketPrice()

        df = pd.DataFrame(columns='STRIKE RIGHT EXPIRATION SYMBOL bid ask'.split())

        df['STRIKE'] = [c.strike for c in contracts]
        df['RIGHT'] = [c.right for c in contracts]
        df['EXPIRATION'] = [c.lastTradeDateOrContractMonth for c in contracts]
        df['SYMBOL'] = [c.symbol for c in contracts]
        contract2Row = {c: i for (i, c) in enumerate(contracts)}
        df['STRIKE'] = df['STRIKE'].astype(int)

        for t in tickers:
            row = contract2Row[t.contract]
            df.iloc[row, 4:] = (t.bid, t.ask)

        df['mid'] = (df['bid'] + df['ask']) / 2
        df['OPTION_REF'] = df['SYMBOL'].str.ljust(6) + df.EXPIRATION
        df['R_REF'] = [c.localSymbol for c in contracts]
        df['UNDLY_PRICE'] = spxValue
        df['TRADE_DT'] = now.strftime("%Y%m%d")
        df['TRADE_TIME'] = now.strftime("%H:%M:%S")

        # display(df)
        df_calls = df[df['RIGHT'] == 'C']
        df_puts = df[df['RIGHT'] == 'P']
        df_puts = df_puts.rename(columns={'R_REF': 'PUT_REF'})

        df_calls = df_calls[['STRIKE', 'R_REF', 'mid', 'OPTION_REF']]
        df_calls = df_calls.rename(columns={'mid': 'CALL_MID'})
        df_calls = df_calls.rename(columns={'R_REF': 'CALL_REF'})

        df_exp = df_puts.merge(df_calls, on=["OPTION_REF", "STRIKE"], how='left')
        df_exp['CALL_MID'] = pd.to_numeric(df_exp['CALL_MID'])
        df_exp = df_exp.rename(columns={'mid': 'PUT_MID'})
        df_exp = df_exp.rename(columns={'SYMBOL': 'UNDLY'})
        df_exp = df_exp.drop(['bid', 'ask'], axis=1)
        df_exp = df_exp.sort_values(['TRADE_DT', 'TRADE_TIME', 'OPTION_REF', 'STRIKE'], ascending=False)
        df_exp.reset_index(drop=True, inplace=True)

        cols = ["TRADE_DT", "TRADE_TIME", "UNDLY", "UNDLY_PRICE", "OPTION_REF", "STRIKE", "CALL_REF", "CALL_MID",
                "PUT_REF", "PUT_MID"]
        df_exp = df_exp[cols]
        # df_exp = df_exp.query("CALL_MID>=0 & PUT_MID>=0" )  # delete negative prices bad data

        return df_exp

    def start_streaming(tickers, spx_ticker, contracts, wait_sec=0.1):
        # timeout = time.time() + 30  # timeout loop after 30s
        # while time.time() < timeout:
        print('starting live data stream to Excel...')
        while True:
            ib.sleep(wait_sec)
            #     ib.pendingTickersEvent(tickers)
            #     ib.sleep(10)
            df = update_price(tickers, spx_ticker, contracts)
            sht1.range('A1').options(index=False).value = df


    signal.signal(signal.SIGINT, signal_handler)
# create ib_insync IB() instance and connect to TWS
    ib = IB()
    ib.sleep(0.1)
    conn_tws()
    ib.sleep(0.1)
    ib.client.MaxRequests = 48

    df_lib = get_libor_func()
    print('Libor download finished')

    tickers, spx_ticker, contracts = get_tickers(min_dte, max_dte, strike_distance, strike_range)
    print('connecting to Excel workbook...')
    wb = xw.Book(workbook)
    sht1 = wb.sheets['FM_FEED']
    sht2 = wb.sheets['RFRATE']
    print('streaming Libor data to Excel...')
    sht2.range('c1').options(index=False).value = df_lib

    start_streaming(tickers, spx_ticker, contracts, wait_sec)


if __name__ == "__main__":
    stream_data()
