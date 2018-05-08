import atexit
import csv
import numpy as np
import os
import pandas as pd
import pandas.io.formats.excel
import requests
from requests.adapters import HTTPAdapter
import requests_cache
from requests.packages.urllib3.util.retry import Retry
from statistics import mean
import xlsxwriter

def retry_session(url, error_codes):
  session = requests.Session()
  retry = Retry(
      total=12,
      backoff_factor=0.1,
      method_whitelist=('GET', 'POST'),
      status_forcelist=error_codes
  )
  adapter = HTTPAdapter(max_retries=retry)
  session.mount(url, adapter)
  return session

def read_input_file(input_filename):
  try:
    input_df = pd.read_csv(input_filename, na_filter=False)
  except:
    print_error_message_and_exit('The program encountered an error while trying to read the input file.  Please make sure there is a file named "' + input_filename + '" in the same directory as the program file named "' + os.path.basename(__file__) + '", and try running the program again.')
  return input_df

def print_error_message_and_exit(error_message):
  print(error_message)
  raise SystemExit

def format_columns(columns):
  new_columns = []
  previous_column = None
  
  for current_column in columns:
    current_column = current_column.lower().replace(' ', '_').strip()
    current_column = current_column.replace('_in_', '_')
    if current_column.startswith('cur.'):
      current_column = previous_column + '_currency'
    new_columns.append(current_column)
    previous_column = current_column   
    
  return new_columns

def get_primary_value_currency(columns):
  primary_value_currency = None
  
  for column in columns:
    if column.startswith('buy_value_'):
      currency = column.split('_')[-1].upper()
      if get_currency_is_fiat(currency):
        primary_value_currency = currency

  if not primary_value_currency:
    print_error_message_and_exit('The input file does not have a buy value column in a supported fiat currency.  Please provide an input file with a buy value column in one of the following fiat currencies and run the program again: ' + ', '.join(fiat_currencies) + '.')
  return primary_value_currency

def check_for_required_columns(columns):
  primary_value_currency = get_primary_value_currency(columns).lower()

  required_columns = ['type', 'buy', 'buy_currency', 'buy_value_' + primary_value_currency, 'sell', 'sell_currency', 'sell_value_' + primary_value_currency, 'exchange', 'comment', 'trade_date']

  missing_columns = list(set(required_columns).difference(columns))
  
  if missing_columns:
    print_error_message_and_exit('The input file is missing the following required column(s): ' + ', '.join(missing_columns) + '.  Please correct the input file and run the program again.')  

def format_values(input_df):
  primary_value_currency = get_primary_value_currency(input_df.columns).lower()
  
  input_df['type'] = input_df['type'].astype(str)
  input_df['buy'] = input_df['buy'].astype(str).replace('-', '0').astype(float)
  input_df['buy_currency'] = input_df['buy_currency'].astype(str)
  input_df['buy_value_' + primary_value_currency] = input_df['buy_value_' + primary_value_currency].astype(float)
  input_df['sell'] = input_df['sell'].astype(str).replace('-', '0').astype(float)
  input_df['sell_currency'] = input_df['sell_currency']
  input_df['sell_value_' + primary_value_currency] = input_df['sell_value_' + primary_value_currency].astype(float)
  input_df['exchange'] = input_df['exchange'].astype(str)
  input_df['comment'] = input_df['comment'].astype(str)
  input_df['trade_date'] = pd.to_datetime(input_df['trade_date'], format='%d.%m.%Y %H:%M')
  input_df = input_df.round(internal_decimal_places)
  input_df.fillna('', inplace=True)
  
  input_df['trade_value_' + primary_value_currency] = input_df.apply(lambda row : set_primary_trade_value(row, primary_value_currency), axis=1)
  
  input_df['buy_currency_is_fiat'] = input_df['buy_currency'].apply(lambda x : get_currency_is_fiat(x))
  
  input_df['sell_currency_is_fiat'] = input_df['sell_currency'].apply(lambda x : get_currency_is_fiat(x))
  
  return input_df
  
def set_primary_trade_value(row, primary_value_currency):
  buy_value = row['buy_value_' + primary_value_currency]
  sell_value = row['sell_value_' + primary_value_currency]

  result = sell_value
  if not sell_value or sell_value == 0:
    result = buy_value
  else:
    result = set_trade_value(row['buy'], row['buy_currency'], row['sell_value_' + primary_value_currency], primary_value_currency)
    result = set_trade_value(row['sell'], row['sell_currency'], row['sell_value_' + primary_value_currency], primary_value_currency)
    
  return result

def set_trade_value(quantity, currency, trade_value, trade_value_currency):
  currency = currency.lower()
  trade_value_currency = trade_value_currency.lower()
  
  if currency == trade_value_currency:
    result = quantity
  else:
    result = trade_value
  return result

def get_currency_is_fiat(currency):
  return currency.upper() in fiat_currencies
  
def create_buy_or_sell_df(input_df, side):  
  primary_value_currency = get_primary_value_currency(input_df.columns).lower()
  df = input_df.loc[(input_df[side] != 0) & (input_df[side + '_currency_is_fiat'] == False), [side, side + '_currency', 'trade_value_' + primary_value_currency, 'exchange', 'comment', 'trade_date']]

  return df

def check_for_valid_buy_and_sell_quantities(buy_df, sell_df):
  for currency in sell_df['sell_currency'].unique():
    if sell_df.loc[sell_df['sell_currency'] == currency, 'sell'].sum().round(internal_decimal_places) > buy_df.loc[buy_df['buy_currency'] == currency, 'buy'].sum().round(internal_decimal_places):
      print_error_message_and_exit('The units sold of ' + currency + ' exceed the units acquired.  Please correct the input file and try again.')

def get_value_columns(value_currencies):
  value_column_prefixes = ['buy_value_', 'sell_value_', 'gain_loss_']

  value_columns = [prefix + currency.lower() for currency in value_currencies for prefix in value_column_prefixes]
  
  return value_columns

def create_buy_and_sell_match_df(buy_df, sell_df, value_currencies, session):
  primary_value_currency = value_currencies[0].lower()

  buy_and_sell_match_df = pd.DataFrame(columns=['currency', 'quantity', 'buy_date', 'sell_date', 'buy_value_' + primary_value_currency, 'sell_value_' + primary_value_currency, 'buy_exchange', 'sell_exchange', 'buy_comment', 'sell_comment'])

  while len(sell_df.index) > 0:
    sell_row_index = sell_df['trade_date'].idxmin()
    sell_row = sell_df.loc[sell_row_index]
    sell_currency = sell_row['sell_currency']
    buy_row_index = buy_df.loc[buy_df['buy_currency'] == sell_currency, 'trade_date'].idxmin()
    buy_row = buy_df.loc[buy_row_index]
    sell_date = sell_row['trade_date']
    buy_date = buy_row['trade_date']
    
    if sell_date < buy_date:
      print_error_message_and_exit('Sell for ' + sell_currency + ' on ' + str(sell_date) + ' cannot be matched with a buy.  The closest buy occurred at a later date: ' + str(buy_date) + '.  Please correct input file and try again.')
    
    buy_quantity = buy_row['buy']
    sell_quantity = sell_row['sell']
    match_quantity = min(buy_quantity, sell_quantity)
    
    buy_match_value = calculate_trade_match_value(match_quantity, buy_quantity, buy_row['trade_value_' + primary_value_currency])
    sell_match_value = calculate_trade_match_value(match_quantity, sell_quantity, sell_row['trade_value_' + primary_value_currency])

    if sell_row['comment'].lower() != 'gift':
      buy_and_sell_match_df.loc[len(buy_and_sell_match_df.index)] = [sell_currency, match_quantity, buy_date, sell_date, buy_match_value, sell_match_value, buy_row['exchange'], sell_row['exchange'], buy_row['comment'], sell_row['comment']]
  
    subtract_match(buy_df, buy_row_index, match_quantity, buy_match_value, primary_value_currency, 'buy')
    subtract_match(sell_df, sell_row_index, match_quantity, sell_match_value, primary_value_currency, 'sell')

  buy_and_sell_match_df = pd.concat([buy_and_sell_match_df, buy_df.rename(columns={'buy':'quantity', 'buy_currency':'currency', 'trade_value_usd':'buy_value_usd', 'exchange':'buy_exchange', 'comment':'buy_comment', 'trade_date':'buy_date'})], ignore_index=True)
  
  for value_currency in value_currencies[1:]:
    value_currency = value_currency.lower()
    for side in ['buy', 'sell']:
      primary_value_column = side + '_value_' + primary_value_currency
      value_column = side + '_value_' + value_currency
      trade_date_column = side + '_date'

      buy_and_sell_match_df[value_column] = buy_and_sell_match_df.loc[buy_and_sell_match_df[trade_date_column].notnull()].apply(lambda row : convert_historical_trade_value(row[primary_value_column], primary_value_currency, value_currency, row[trade_date_column], session), axis=1)

      buy_and_sell_match_df[value_column] = buy_and_sell_match_df.loc[buy_and_sell_match_df[trade_date_column].notnull()].apply(lambda row : set_trade_value(row['quantity'], row['currency'], row[value_column], value_currency), axis=1)
  
  buy_and_sell_match_df = add_gain_loss_to_df(buy_and_sell_match_df, value_currencies)
  
  buy_and_sell_match_df = buy_and_sell_match_df[['currency', 'quantity', 'buy_date', 'sell_date'] + get_value_columns(value_currencies) + ['buy_exchange', 'sell_exchange', 'buy_comment', 'sell_comment']]

  buy_and_sell_match_df.sort_values(by=['sell_date', 'buy_date'], ascending=False, inplace=True)

  return buy_and_sell_match_df

def calculate_trade_match_value(match_quantity, trade_quantity, trade_value):
  return round((match_quantity / trade_quantity) * trade_value, internal_decimal_places)

def subtract_match(df, index, match_quantity, match_value, primary_value_currency, side):
  value_column = 'trade_value_' + primary_value_currency
  df.loc[index, side] = round(df.loc[index, side] - match_quantity, internal_decimal_places)
  df.loc[index, value_column] = round(df.loc[index, value_column] - match_value, internal_decimal_places)

  if df.loc[index, side] == 0:
    df.drop(index, inplace=True)
  
  return df

def get_cryptocompare_average_hourly_price(from_currency, to_currency, timestamp_value, session):
  unix_time = str(int(timestamp_value.timestamp()))
  from_currency = from_currency.upper()
  to_currency = to_currency.upper()
  result = None
  
  try:
    response = get_request(session, cryptocompare_api_base_url + 'histohour?fsym=' + from_currency + '&tsym=' + to_currency + '&limit=1&toTs=' + unix_time)

    if response.json()['Response'] == 'Success':
      result = mean([price['close'] for price in response.json()['Data']])
    else:
      print_error_message_and_exit('The program encountered an error while trying to convert ' + from_currency + ' to ' + to_currency + '.  It is likely that CryptoCompare does not have data for one of these currencies.  Please select a different currency conversion pair and try running the program again.')
  except:
    print_error_message_and_exit('The program encountered an error while trying to retrieve historical prices from the CryptoCompare API.  Please try running the program again later.')
  return result

def get_request(session, url):
  return session.get(url, timeout=5)
  
def add_gain_loss_to_df(df, value_currencies):
  for currency in value_currencies:
    currency = currency.lower()
    df['gain_loss_' + currency] = df['sell_value_' + currency] - df['buy_value_' + currency]
    
  return df

def create_realized_totals_df(buy_and_sell_match_df, pivot_values, margins_name):
  buy_and_sell_realized_df = buy_and_sell_match_df.copy()
  buy_and_sell_realized_df['sell_year'] = buy_and_sell_realized_df['sell_date'].dt.year
  buy_and_sell_realized_df = buy_and_sell_realized_df.loc[buy_and_sell_realized_df['sell_date'].notnull()]
  pivot_index = ['sell_year', 'currency']
  
  realized_totals_df = create_totals_df(buy_and_sell_realized_df, pivot_index, pivot_values, True, margins_name)
  
  return realized_totals_df
  
def create_totals_df(df, pivot_index, pivot_values, margins, margins_name):
  totals_df = pd.pivot_table(df, index=pivot_index, values=pivot_values, aggfunc=np.sum, margins=margins, margins_name=margins_name)

  totals_df.reset_index(inplace=True)
  totals_df = totals_df[pivot_index + pivot_values]
  first_column = totals_df.columns[0]
  totals_df.loc[totals_df[first_column] == margins_name, 'quantity'] = np.NaN
  
  return totals_df

def create_totals_per_unit_df(totals_df, columns, margins_name):
  totals_df = totals_df.copy()
  
  for column in columns:
    totals_df[column] = totals_df[column] / totals_df['quantity']

  first_column = totals_df.columns[0]
  totals_df = totals_df[totals_df[first_column] != 'Total']
  
  return totals_df

def create_unrealized_totals_df(buy_and_sell_match_df, pivot_values, value_currencies, margins_name, session):
  unrealized_totals_df = create_totals_df(buy_and_sell_match_df.loc[buy_and_sell_match_df['sell_date'].isnull()], ['currency'], pivot_values, False, margins_name)

  coinmarketcap_id_dict = get_coinmarketcap_ids(session)
  
  for currency in value_currencies:
    currency = currency.lower()
    unrealized_totals_df['sell_value_' + currency] = unrealized_totals_df.apply(lambda row : row['quantity'] * get_coinmarketcap_current_price(row['currency'], currency, coinmarketcap_id_dict, session), axis=1)
  
  unrealized_totals_df = add_gain_loss_to_df(unrealized_totals_df, value_currencies)

  unrealized_totals_df = create_totals_df(unrealized_totals_df, ['currency'], pivot_values, True, margins_name)
  
  return unrealized_totals_df

def get_coinmarketcap_ids(session):
  try:
    response = get_request(session, coinmarketcap_api_base_url + 'ticker/?limit=0')
  except:
    print_error_message_and_exit('The program encountered an error while trying to retrieve coin IDs from the CoinMarketCap API.  Please try running the program again later.')

  coinmarketcap_id_dict = {}
    
  for coin in response.json():
    coinmarketcap_id_dict[coin['symbol'].lower()] = coin['id'].lower()
    
  coinmarketcap_id_dict['cpc'] = 'cpchain'

  return coinmarketcap_id_dict

def get_coinmarketcap_current_price(from_currency, to_currency, coinmarketcap_id_dict, session):
  coinmarketcap_id = coinmarketcap_id_dict.get(from_currency.lower())

  if coinmarketcap_id:
    to_currency = to_currency.lower()
    try:
      with requests_cache.disabled():
        response = get_request(session, coinmarketcap_api_base_url + 'ticker/' + coinmarketcap_id +  '/?convert=' + to_currency)
    except:
      print_error_message_and_exit('The program encountered an error while trying to retrieve current prices from the CoinMarketCap.com API.  Please try running the program again later.')

    current_price = float(response.json()[0]['price_' + to_currency])
  else:
    print('CoinMarketCap does not have the current price for ' + from_currency + '.  The currency will have a current value of zero in the output file.')
    current_price = 0
      
  return current_price

def format_excel_sheet(df, sheet):
  max_width_list = [len(column) + 2 for column in df.columns]
  for i, width in enumerate(max_width_list):
    sheet.set_column(i, i, width)
  
  sheet.autofilter(0, 0, len(df.index) - 1, len(df.columns) - 1)
  sheet.freeze_panes(1, 0)

def convert_historical_trade_value(from_value, from_currency, to_currency, trade_date, session):
  return from_value * get_cryptocompare_average_hourly_price(from_currency, to_currency, trade_date, session)
  
def write_excel_sheet(df, writer, sheet_name):
  df.to_excel(writer, sheet_name = sheet_name, index=False)
  format_excel_sheet(df, writer.sheets[sheet_name])
  
  return writer

def output_excel_file(writer, excel_output_filename):
  try:
    writer.save()
  except:
    print_error_message_and_exit('The program encountered an error while trying to write the Excel output file named "' + excel_output_filename + '".  Please ensure this file is closed and try running the program again.')

def main():
  cryptocompare_session = retry_session(cryptocompare_api_base_url, error_codes)
  coinmarketcap_session = retry_session(coinmarketcap_api_base_url, error_codes)
  
  input_df = read_input_file(cointracking_input_filename)
  original_input_df = input_df.copy()
  input_df.columns = format_columns(input_df.columns)
  
  primary_value_currency = get_primary_value_currency(input_df.columns)
  value_currencies = [primary_value_currency, 'BTC', 'ETH']
  
  check_for_required_columns(input_df.columns)
  
  input_df = format_values(input_df)
  
  buy_df = create_buy_or_sell_df(input_df, 'buy')
  sell_df = create_buy_or_sell_df(input_df, 'sell')

  check_for_valid_buy_and_sell_quantities(buy_df, sell_df)
  
  buy_and_sell_match_df = create_buy_and_sell_match_df(buy_df, sell_df, value_currencies, coinmarketcap_session)
  
  value_columns = get_value_columns(value_currencies)
  pivot_values = ['quantity'] + value_columns
  margins_name = 'Total'
  
  realized_totals_df = create_realized_totals_df(buy_and_sell_match_df, pivot_values, margins_name)
  realized_totals_per_unit_df = create_totals_per_unit_df(realized_totals_df, value_columns, margins_name)

  unrealized_totals_df = create_unrealized_totals_df(buy_and_sell_match_df, pivot_values, value_currencies, margins_name, coinmarketcap_session)
  unrealized_totals_per_unit_df = create_totals_per_unit_df(unrealized_totals_df, value_columns, margins_name)
  
  buy_and_sell_match_df = buy_and_sell_match_df.round(2)
  realized_totals_df = realized_totals_df.round(2)
  realized_totals_per_unit_df = realized_totals_per_unit_df.round(8)
  unrealized_totals_df = unrealized_totals_df.round(2)
  unrealized_totals_per_unit_df = unrealized_totals_per_unit_df.round(8)
  
  writer = pd.ExcelWriter(excel_output_filename, engine='xlsxwriter')
  write_excel_sheet(original_input_df, writer, 'input')
  write_excel_sheet(buy_and_sell_match_df, writer, 'buy_and_sell_match')
  write_excel_sheet(realized_totals_df, writer, 'realized_totals')
  write_excel_sheet(realized_totals_per_unit_df, writer, 'realized_totals_per_unit')
  write_excel_sheet(unrealized_totals_df, writer, 'unrealized_totals')
  write_excel_sheet(unrealized_totals_per_unit_df, writer, 'unrealized_totals_per_unit')
  
  output_excel_file(writer, excel_output_filename)

  print('Successfully generated ' + excel_output_filename)
  
fiat_currencies = fiat_currencies = ['AED', 'ARS', 'AUD', 'BRL', 'CAD', 'CHF', 'CLP', 'CNY', 'CZK', 'DKK', 'EUR', 'GBP', 'HKD', 'HUF', 'IDR', 'ILS', 'INR', 'JPY', 'KRW', 'MXN', 'MYR', 'NOK', 'NZD', 'PHP', 'PKR', 'PLN', 'RON', 'RUB', 'SEK', 'SGD', 'THB', 'TRY', 'TWD', 'UAH', 'USD', 'ZAR']
  
internal_decimal_places = 8

pandas.io.formats.excel.header_style = None
pd.options.mode.chained_assignment = None

cointracking_input_filename = 'CoinTracking · Trade List.csv'
excel_output_filename = 'portfolio_data.xlsx'

requests_cache.install_cache(allowable_codes=(200,))
error_codes = set([400, 401, 403, 404, 429, 500, 502, 503, 504])
cryptocompare_api_base_url = 'https://min-api.cryptocompare.com/data/'
coinmarketcap_api_base_url = 'https://api.coinmarketcap.com/v1/'

if __name__ == '__main__':
  main()