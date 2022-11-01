import requests
import os
import json
from dateutil import parser
from datetime import datetime
import pandas as pd

import logging
import pytz
from typing import Optional

from botocore.exceptions import ClientError
import boto3
from nltk.sentiment import SentimentIntensityAnalyzer
import pandas as pd
import psycopg2
import psycopg2.extras

# To set your environment variables in your terminal run the following line:
# export 'BEARER_TOKEN'='<your_bearer_token>'
#This bearer token is to be generated after developing Twitter developer account.
#Below is a dummy bearer token 
bearer_token = 'AAAAasasqsqsaAAAAAAAAAAAAAEQBZQEAAAgPfqwSbhjjit0rdNX%2B%2BONdyWs%3Dp0GBN5ItZNXFpsv5JYlaWeSOQO0gg0piL10FwRkzjZ9J0dNR5p'
#Token changed a bit

try:
    sia = SentimentIntensityAnalyzer()
except LookupError:
    import nltk
    # in lambda you can only write to /tmp folder
    # nltk needs to download data to run a model
    nltk.download('vader_lexicon', download_dir='/tmp')
    # nltk will look for the downloaded data to run SentimentIntensityAnalyzer
    nltk.data.path.append("/tmp")
    sia = SentimentIntensityAnalyzer()

def create_url():
    # Replace with user ID below
    #user_id = 2244994945
    user_id = 1652541  # For reuters
    return "https://api.twitter.com/2/users/{}/tweets".format(user_id)


def get_params():
    # Tweet fields are adjustable.
    # Options include:
    # attachments, author_id, context_annotations,
    # conversation_id, created_at, entities, geo, id,
    # in_reply_to_user_id, lang, non_public_metrics, organic_metrics,
    # possibly_sensitive, promoted_metrics, public_metrics, referenced_tweets,
    # source, text, and withheld
    return {"tweet.fields": "created_at"}


def bearer_oauth(r):
    """
    Method required by bearer token authentication.
    """
    r.headers["Authorization"] = f"Bearer {bearer_token}"
    r.headers["User-Agent"] = "v2UserTweetsPython"
    return r

def connect_to_endpoint(url, params):
    response = requests.request("GET", url, auth=bearer_oauth, params=params)
    print(response.status_code)
    if response.status_code != 200:
        raise Exception(
            "Request returned an error: {} {}".format(
                response.status_code, response.text
            )
        )
    return response.json()

def extract_fields(tweet: dict) -> dict:
    '''
    Arbitrary decision to save only some fields of the tweet,
    store them in a different dictionary form which
    is convenient for saving them later
    '''
    author = 'Reuters'
    time_created = parser.parse(tweet['created_at'])
    text = tweet['text']
    return dict(author=author, timestamp=time_created, text=text)

def _get_sentiment(string: str) -> float:
    '''
    make sure the score is between -1 (very negative) and 1 (very positive)
    '''
    # sia is the SentimentIntensityAnalyzer object which gives a positive and negative score
    score = sia.polarity_scores(string)
    # we want only 1 score so the negative sentiment will be a negative score 
    # and likewise for the positive
    score = score['neg'] * -1 + score['pos']
    return score

def add_sentiment_score(tweet: dict) -> dict:
    tweet['sentiment_score'] = _get_sentiment(tweet['text'])
    return tweet


def upload_file_to_s3(local_file_name: str,
                      bucket: str,
                      s3_object_name: Optional[str]=None):
    """Upload a file to an S3 bucket

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param s3_object_name: If not specified then file_name is used
    :return: True if file was uploaded, else False
    """

    # If S3 object_name was not specified, use file_name
    if s3_object_name is None:
        s3_object_name = local_file_name

    # Upload the file
    s3_client = boto3.client('s3')
    try:
        s3_client.upload_file(local_file_name, bucket, s3_object_name)
    except ClientError as e:
        logging.error(e)
        return False
    return True

def get_db_connection() -> psycopg2.extensions.connection:
    # to connect to DB, use the parameters and password that define it
    conn = psycopg2.connect(
                            user="postgres",
                            password=os.environ['DB_PASSWORD'],
                            host=os.environ['DB_HOST'],
                            port="5432",
                            connect_timeout=1)
    return conn

def insert_data_in_db(df: pd.DataFrame,
                      conn: psycopg2.extensions.connection,
                      table_name: str = 'tweets_analytics') -> None:
    # you need data and a valid connection to insert data in DB
    are_data = len(df) > 0
    if are_data and conn is not None:
        try:
            cur = conn.cursor()
            # to perform a batch insert we need to reshape the data in 2 strings with the column names and their values
            df_columns = list(df.columns)
            columns = ",".join(df_columns)

            # create VALUES('%s', '%s",...) one '%s' per column
            values = "VALUES({})".format(",".join(["%s" for _ in df_columns]))

            # create INSERT INTO table (columns) VALUES('%s',...)
            # here the final 2 strings are created
            insert_string = "INSERT INTO {} ({}) {}"
            insert_stmt = insert_string.format(table_name, columns, values)
            psycopg2.extras.execute_batch(cur, insert_stmt, df.values)
            conn.commit()
            print('succesful update')

        except psycopg2.errors.InFailedSqlTransaction:
            # if the transaction fails, rollback to avoid DB lock problems
            logging.exception('FAILED transaction')
            cur.execute("ROLLBACK")
            conn.commit()

        except Exception as e:
            # if the transaction fails, rollback to avoid DB lock problems
            logging.exception(f'FAILED  {str(e)}')
            cur.execute("ROLLBACK")
            conn.commit()
        finally:
            # close the DB connection after this
            cur.close()
            conn.close()
    elif conn is None:
        raise ValueError('Connection to DB must be alive!')
    elif len(df) == 0:
        raise ValueError('df has 0 rows!')


def is_recent(tweet):
    time_created = parser.parse(tweet['created_at'])
    now = datetime.now(tz=pytz.UTC)
    # converts time to minutes as the function takes minutes as argument
    seconds_diff = (now-time_created).seconds
    minutes_diff = seconds_diff/60
    is_recent_tweet = minutes_diff <= 5
    return is_recent_tweet

def convert_timestamp_to_int(tweet: dict) ->dict:
    '''datetime object are not serializable for json,
    so we need to convert them to unix timestamp'''
    tweet = tweet.copy()
    tweet['timestamp'] = tweet['timestamp'].timestamp()
    return tweet
    
def lambda_handler(event, context):
    try:
        url = create_url()
        params = get_params()
        json_response = connect_to_endpoint(url, params)
        #print(json.dumps(json_response, indent=4, sort_keys=True))
        #print(json.dumps(json_response, indent=4, sort_keys=True))
        #df = json.dumps(json_response, indent=4, sort_keys=True)

        recent_tweets = [tweet for tweet in json_response['data']
                         if is_recent(tweet)]  
        recent_tweets = [extract_fields(tweet) for tweet in recent_tweets]
        recent_tweets = [add_sentiment_score(tweet) for tweet in recent_tweets]
        now_str = datetime.now(tz=pytz.UTC).strftime('%d-%m-%Y-%H:%M:%S')
        filename = f'{now_str}.json'
        ##Change this path for lambda
        output_path_file = f'/tmp/{filename}'
            # in lambda files need to be dumped into /tmp folder
        with open(output_path_file, 'w') as fout:
            tweets_to_save = [convert_timestamp_to_int(tweet)
                              for tweet in recent_tweets]
            json.dump(tweets_to_save , fout)
            print(tweets_to_save)

        upload_file_to_s3(local_file_name=output_path_file,
                          bucket='my-tweet-data-lake2',
                          s3_object_name=f'raw-messages/{filename}')
        
        tweets_df = pd.DataFrame(recent_tweets)
        conn = get_db_connection()
        insert_data_in_db(df=tweets_df, conn=conn, table_name='tweets_analytics')
    except Exception as e:
        logging.exception('Exception occured \n')
    # add_messages_to_db(df=tweets_df, conn=conn)
    print('Lambda executed succesfully!')

     

        
        

if __name__ == "__main__":
    lambda_handler({}, {})