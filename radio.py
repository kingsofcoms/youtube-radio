import subprocess
import random
import sqlite3
import sys
import time
import datetime
import atexit
import asyncio
import functools
from threading import Thread, currentThread
from dateutil import parser
from os import listdir
from os.path import isfile, join

import requests
from oauth2client.tools import argparser
from youtube_api import insert_broadcast, insert_stream, bind_broadcast, get_authenticated_service
conn = sqlite3.connect('radio.db')
conn.row_factory = sqlite3.Row

config = {}

def run(args):
    started = False
    query = """SELECT url
               FROM songs
               ORDER BY RANDOM()
               LIMIT 1
            """
    config['staging_url'] = list(conn.cursor().execute(query))[0]['url']
    delete = subprocess.Popen(['rm', '-rf', '/tmp/staging.wav'], stdout=subprocess.PIPE)
    delete.wait()
    download = subprocess.Popen(['youtube-dl', '--extract-audio', '--audio-format', 'wav', '-o', '/tmp/staging.wav', config['staging_url']], stdout=subprocess.PIPE)
    download.wait()

    process_start = time.time()
    config['youtube'] = get_authenticated_service(args)
    found = None
    broadcasts = config['youtube'].liveBroadcasts().list(part='snippet,id,contentDetails,status', mine=True).execute()['items']
    for broadcast in broadcasts:
        if broadcast['snippet']['title'] == config['broadcast-title']:
            if broadcast['status']['lifeCycleStatus'] == 'complete':
                config['youtube'].liveBroadcasts().delete(id=broadcast['id'])
            else:
                found = broadcast
                break
    if found:
        started = True
        config['broadcast'] = found
    else:
        config['broadcast'] = insert_broadcast(config['youtube'], args)
    found = None
    streams = config['youtube'].liveStreams().list(part='snippet,id,cdn,status', mine=True).execute()['items']
    for stream in streams:
        if stream['snippet']['title'] == config['stream-title']:
            found = stream
            break
    if found:
        config['stream'] = found
    else:
        config['stream'] = insert_stream(config['youtube'], args)
    bind_broadcast(config['youtube'], config['broadcast']['id'], config['stream']['id'])
    stream_url = config['stream']['cdn']['ingestionInfo']['ingestionAddress'] + '/' + config['stream']['cdn']['ingestionInfo']['streamName']
    atexit.register(radio_teardown)
    config['chat_poll'] = Thread(target=chat_poll)
    config['chat_poll'].start()
    while True:
        swap = subprocess.Popen(['mv', '/tmp/staging.wav', '/tmp/current.wav'], stdout=subprocess.PIPE)
        config['current_url'] = config['staging_url']
        swap.wait()
        if swap.returncode != 0:
            print('swap failed')
            break
        start = time.time()
        if start > process_start + 60 * 60 * 24:
            print('hit 24 hour time limit')
            break
        image = random.choice([f for f in listdir('images') if isfile(join('images', f))])
        stream = subprocess.Popen(['./stream.sh', image, stream_url])
        time.sleep(15)
        if not started:
            response = config['youtube'].liveBroadcasts().transition(broadcastStatus= 'live', id=config['broadcast']['id'], part='snippet,status,contentDetails').execute()
        query = """SELECT url
                   FROM songs
                   ORDER BY RANDOM()
                   LIMIT 1
                """
        config['staging_url'] = list(conn.cursor().execute(query))[0]['url']
        delete = subprocess.Popen(['rm', '-rf', '/tmp/staging.wav'], stdout=subprocess.PIPE)
        delete.wait()
        if delete.returncode != 0:
            print('delete failed')
            break
        download = subprocess.Popen(['youtube-dl', '--extract-audio', '--audio-format', 'wav', '-o', '/tmp/staging.wav', config['staging_url']], stdout=subprocess.PIPE)
        download.wait()
        if download.returncode != 0:
            print('download failed')
            break
        stream.wait()
        if stream.returncode != 0:
            print('stream failed')
            break
        started = True

def chat_poll():
    config['poll_conn'] = sqlite3.connect('radio.db')
    config['poll_conn'].row_factory = sqlite3.Row
    config['index'] = 0
    t = currentThread()
    while getattr(t, "do_run", True):
        response = config['youtube'].liveChatMessages().list(liveChatId=config['broadcast']['snippet']['liveChatId'], part='snippet').execute()
        messages = response['items']
        if not config['index']:
            config['index'] = len(messages)
        parse_messages(messages)
        config['index'] = len(messages)
        time.sleep(response['pollingIntervalMillis']/1000)
    print("Closing")

def parse_messages(messages):
    upvotes = 0
    downvotes = 0
    add_tracks = []
# limited to 500 messages but fuck implementing pagination handling for a jerk off app
    for message in messages[config['index']:]:
        if 'textMessageDetails' in message['snippet'] and 'messageText' in message['snippet']['textMessageDetails']:
            if '++' in message['snippet']['textMessageDetails']['messageText'] and message['snippet']['type'] == 'textMessageEvent' and config['enable-upvotes']:
                print('Upvoted!')
                upvotes = upvotes + 1
            elif '--' in message['snippet']['textMessageDetails']['messageText'] and message['snippet']['type'] == 'textMessageEvent' and config['enable-downvotes']:
                print('Downvoted!')
                downvotes = downvotes + 1
            elif message['snippet']['textMessageDetails']['messageText'][0:4] == '!add' and message['snippet']['type'] == 'textMessageEvent' and config['enable-adding']:
                print('Added!')
                add_tracks.append(message['snippet']['textMessageDetails']['messageText'].split(' ')[1])
                response_text = "Added {} to the playlist!".format(message['snippet']['textMessageDetails']['messageText'].split(' ')[1])
                body = {"snippet":
                          {"liveChatId":config['broadcast']['snippet']['liveChatId'],
                           'type': 'textMessageEvent',
                           'textMessageDetails':{'messageText': response_text}
                          }
                        }
                config['index'] = config['index'] + 1
                config['youtube'].liveChatMessages().insert(part='snippet', body=body).execute()
    if upvotes:
        upvote(upvotes)
    if downvotes:
        downvote(downvotes)
    if len(add_tracks):
        for track in add_tracks:
            upvote(url=track)
        
def radio_teardown():
    config['chat_poll'].do_run = False
    config['chat_poll'].join()
#    config['youtube'].liveBroadcasts().delete(id=config['broadcast']['id']).execute()
#    config['youtube'].liveStreams().delete(id=config['steam']['id']).execute()

def init():
    # db structure is a little weird. rather than having a score properly, the score will be how many rows with that url exist.
    cur = conn.cursor()
    cur.execute("""CREATE TABLE songs (
                     id INTEGER PRIMARY KEY NOT NULL,
                     url TEXT NOT NULL
                   )
                """)
    conn.commit()

def upvote(times=1, url=None):
    if not url:
        url = config['current_url']
    print("{} upvotes for {}".format(times, url))
    cur = config['poll_conn'].cursor()
    cur.executemany("""INSERT INTO songs (url) values(?)""", [(url,)] * times)
    config['poll_conn'].commit()

def downvote(times=1):
    print("{} downvotes for {}".format(times, config['current_url']))
    cur = config['poll_conn'].cursor()
    cur.execute("""DELETE FROM songs WHERE id = (SELECT id FROM songs WHERE url=? LIMIT ?)""", (config['current_url'], times,))
    config['poll_conn'].commit()

if __name__ == "__main__":
    argparser.add_argument("--broadcast-title", help="Broadcast title",
                           default="Nobel Radio")
    argparser.add_argument("--privacy-status", help="Broadcast privacy status",
                           default="unlisted")
    argparser.add_argument("--start-time", help="Scheduled start time",
                           default=datetime.datetime.utcnow().isoformat() + "Z")
    argparser.add_argument("--end-time", help="Scheduled end time",
                           default=(datetime.datetime.utcnow() + datetime.timedelta(hours=24)).isoformat() + "Z")
    argparser.add_argument("--stream-title", help="Stream title",
                           default="Nobel Radio")
    argparser.add_argument("--disable-upvotes", help="Disable Upvotes")
    argparser.add_argument("--disable-downvotes", help="Disable Downvotes")
    argparser.add_argument("--disable-adding", help="Disable Adding Songs")
    args = argparser.parse_args()
    args.noauth_local_webserver = False
    config['broadcast-title'] = args.broadcast_title
    config['stream-title'] = args.stream_title
    config['enable-upvotes'] = not args.disable_upvotes
    config['enable-downvotes'] = not args.disable_downvotes
    config['enable-adding'] = not args.disable_adding
    if len(sys.argv) > 1:
        if sys.argv[1] == 'init':
            init()
        elif sys.argv[1] == 'add':
            upvote(sys.argv[2])
    else:
        run(args)