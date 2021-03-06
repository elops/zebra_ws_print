#!/usr/bin/env python3.9

import asyncio
import logging
import websockets
import base64
import ast
#import simplejson as json
import json
import aiohttp
import urllib
import requests
import yaml
import sys
import re
import traceback
import signal
import time
import os

from aiohttp import web

# globals
open_cfg = b'{ "open" : "v1.config.zebra.com" }'
open_raw = b'{ "open" : "v1.raw.zebra.com" }'

print_spojeno = b"""
    ^XA
    ^LL 200
    ^PW 609
    ^FT78,76^A0N,28,28^FH\^FD zebra.hallochef.net : OK ^FS
    ^XZ
    """

printers = {}
options = {}

def write_pid_to_pidfile(pidfile_path):
    """ Write the PID in the named PID file.

        Get the numeric process ID (“PID”) of the current process
        and write it to the named file as a line of text.

        """
    open_flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    open_mode = 0o644
    pidfile_fd = os.open(pidfile_path, open_flags, open_mode)
    pidfile = os.fdopen(pidfile_fd, 'w')

    # According to the FHS 2.3 section on PID files in /var/run:
    #
    #   The file must consist of the process identifier in
    #   ASCII-encoded decimal, followed by a newline character. For
    #   example, if crond was process number 25, /var/run/crond.pid
    #   would contain three characters: two, five, and newline.

    pid = os.getpid()
    pidfile.write("%s\n" % pid)
    pidfile.close()


def getSerialFromDiscovery(packet):
    """ receives bytes e.g.
        b'{\r\n  "discovery_b64" : "OiwuBAIBAAFaQlIAAFpENDIwLWNoZWxsbwAAAAAAAAAA...
        returns string e.g.
        50J161000398
    """

    # convert packet to dictionary
    packet_dict = ast.literal_eval(packet)
    if 'discovery_b64' in packet_dict.keys():
        return base64.b64decode(packet_dict['discovery_b64'])[188:188+12].decode('utf-8')
    else:
        return None


def new_ws_conn(future):
    """ Prints msg from future """
    printer_id, channel_name = future.result()
    log.info('[{}] [{}] New websocket connection'.format(printer_id, channel_name))


def get_msgid(message):
    """ This function searches string to find MSG UUID or some other indicator
        which would identify type of msg
        input is binary msg e.g.
        b'foo bar baz\n in few lines\n\n ^FX UUID: #1234 test\n'
        output is string which is returned
    """

    matches = re.findall(r'\^FX UUID: #(\d+)', message)
    if len(matches):
        last_id = matches[0]
        log.debug('Message contains ID : {}'.format(last_id))
        return last_id
    else:
        log.warn('No ID found in message : {}'.format(message))
        return 'custom'


def glue_messages(message, full_message):
    """ Glues parts of message
        returns full_message (message glued so far)
        if message is decodable by json.loads set end_of_msg = True
    """

    end_of_msg = False
    message_dict = None
    single_part = False

    if full_message is None:
        single_part = True
        full_message = message
    else:
        full_message += message

    # Check if we can json load 'full_message' ?
    try:
        message_dict = json.loads(full_message)
    except json.decoder.JSONDecodeError:
        log.info('Partial message, trying to glue it together')

    if message_dict is not None:
        end_of_msg = True

    if end_of_msg and not single_part:
        log.info('Finished glueing multi-frame message')

    if end_of_msg and single_part:
        log.debug('Single part message : {}'.format(full_message))

    return (full_message, end_of_msg)


async def consumer(queue, id_queue, message, ws_info, sgd_queue):
    """ This coroutine handles data that came from websocket, parses it
        'message' - incomming data from websocket
        'queue' - fifo queue which is used to feed raw queue (print jobs)
        'id_queue' - fifo queue for tracking which print job was printed
        'sgd_queue' - fifo queue for transporting asyncio.Future()
                      which is used to relay SGD CMD response
        'ws_info' - asyncio.Future() which is used to tell our handler more
                    about peers of this WS connection, essentially tuple
                    ('serial_number', 'channel_name')

    """
    await asyncio.sleep(0.1)
    log.debug('Consumed  {}'.format(message))

    msg_dict = json.loads(message)

    # read data from msg_dict
    if 'unique_id' in msg_dict.keys():
        serial_num = msg_dict['unique_id']
        log.debug('[{}] identified sn from message'.format(serial_num))
    elif 'discovery_b64' in msg_dict.keys():
        log.debug('[PRINTER RESPONSE] : \n{}'.format(message))
    elif 'alert' in msg_dict.keys():
        log.debug('[PRINTER RESPONSE] : \n{}'.format(message))
    else:
        log.info('[PRINTER RESPONSE] : \n{}'.format(message))

    # Get future from queue, if there is future set it
    # Would be a shame to have future waiting if there is one...
    try:
        log.debug('[CONSUMER] Trying to get future from queue')
        sgd_future = sgd_queue.get_nowait()
    except asyncio.queues.QueueEmpty:
        log.debug('[CONSUMER] has no future, likely not a SGD related consumer')
        sgd_future = None

    if sgd_future is not None:
        log.debug('[SETTING FUTURE]')
        sgd_future.set_result(message)

    # Handle discovery message which establishes the main websocket channel
    if 'discovery_b64' in msg_dict.keys():
        serial_num = getSerialFromDiscovery(message)
        # Set future to help identify WS conn
        ws_info.set_result((serial_num,'MAIN'))

        log.debug(' *** MAIN channel established with : {} *** '.format(serial_num))
        # initialize global dictionary for this printer SN
        if serial_num in printers.keys():
            log.info('[{}] [MAIN] looks like global array already knows about us'.format(serial_num))
        else:
            log.info('[{}] [MAIN] Creating new global dict for this printer'.format(serial_num))
            printers[serial_num] = {}
        printers[serial_num]['main'] = queue

        # Open seconday channels if needed
        if 'raw' not in printers[serial_num].keys():
            queue.put_nowait((None, open_raw))
        else:
            log.warning('[{}] [MAIN] Stale RAW channel found, skipping opening new one'.format(serial_num))

        if 'config' not in printers[serial_num].keys():
            queue.put_nowait((None, open_cfg))
        else:
            log.warning('[{}] [MAIN] Stale CONFIG channel found, skipping opening new one'.format(serial_num))


    # Handle establish messages for RAW and CONFIG channel
    if 'channel_name' in msg_dict.keys():
        if 'v1.raw.zebra.com' == msg_dict['channel_name']:
            # Set future to help identify WS conn
            ws_info.set_result( (serial_num, 'RAW') )

            log.debug(' *** RAW channel established with : {} *** '.format(serial_num))
            printers[serial_num]['raw'] = queue
            printers[serial_num]['id_q'] = id_queue
            # Print connected message, and feed 'id_queue' which print job was submitted
            queue.put_nowait((None, print_spojeno))
            await id_queue.put('connected')

        elif 'v1.config.zebra.com' == msg_dict['channel_name']:
            # Set future to help identify WS conn
            ws_info.set_result( (serial_num, 'CONFIG') )

            log.debug(' *** CONFIG channel established with : {} *** '.format(serial_num))
            printers[serial_num]['config'] = queue

            async with aiohttp.request('POST', options['print_job_done'], json={'sn': serial_num, 'job_id': 'connected', 'channel': 'CONFIG' }) as response:
                log.info('RESPONSE : {} - {}'.format(response.status, await response.text()))



    # parse alert messages
    if 'alert' in msg_dict.keys():
        log.debug('ALERT PARSING')
        if 'condition' in msg_dict['alert'].keys():
            # print job done message
            if msg_dict['alert']['condition'] == 'PQ JOB COMPLETED':
                log.debug('ALERT PARSING #PQ JOB COMPLETE')
                printer_sn = msg_dict['alert']['unique_id']
                # Signaling IDs
                id_queue = printers[printer_sn]['id_q']
                try:
                    last_id = await id_queue.get()
                except asyncio.queues.QueueEmpty:
                    log.error('ID Queue was empty, this is not supposed to happen')
                    last_id = 'ERROR'

                log.info('[{}] PRINT JOB : {} DONE'.format(printer_sn, last_id))
                async with aiohttp.request('POST', options['print_job_done'], json={'sn': printer_sn, 'job_id': last_id, 'channel': 'RAW' }) as response:
                    log.info("[{}] PQ JOB ACK RESPONSE  : {} - {}".format(printer_sn, response.status, await response.text()))

            # get data scanned from barcode
            if msg_dict['alert']['condition'] == 'SGD SET':
                log.debug('ALERT PARSING #SGD SET')
                if 'setting_value' in msg_dict['alert'].keys():
                    scanned_data = msg_dict['alert']['setting_value']
                    printer_sn = msg_dict['alert']['unique_id']

                    log.info('[{}] SCANNED DATA : {} '.format(printer_sn, scanned_data))

                    async with aiohttp.request('POST', options['scan_data_url'], json={'data': scanned_data, 'sn': printer_sn}) as response:
                        log.info("[{}] SCANNED DATA : {} ACK {} - {}".format(printer_sn, scanned_data, response.status, await response.text()))


async def producer(queue):
    while True:
        try:
            command = queue.get_nowait()
            log.info('Command retreived from queue : {}'.format(command))
            return command

        except asyncio.queues.QueueEmpty:
            log.debug('Queue is empty exception')
            # lower sleep time will cause faster loops causing more CPU usage
            await asyncio.sleep(0.5)


async def list_printers(request):

    output = ''

    for printer in printers.keys():
        for channel in printers[printer].keys():
            output += "\n[{}] - {}".format(printer, channel)
        output += "\n"
    return web.Response(text=str(output))


async def print_zpl_ng_dummy(request):
    """ This coro only attempts to unpack request """

    buffer = b""
    async for data, end_of_http_chunk in request.content.iter_chunks():
        buffer += data

    post_data = buffer.decode('utf-8')

    log.info(post_data)
    post_data = post_data.replace('\n', '\\n')
    json_data = json.loads(post_data)

    return web.Response(text='DUMMY')


async def print_zpl_ng(request):
    """ This coro receives print job which is relayed to appropriate queue
    """

    # We don't need future for print jobs because it would make printing 
    # a sync/blocking job, instead we will be responding to print jobs with '.'
    # and as print job done confirmation returns reading from id_queue which
    # job was printed and accordingly notify backend
    #print_job_future = asyncio.Future()
    print_job_future = None


    buffer = b""
    async for data, end_of_http_chunk in request.content.iter_chunks():
        buffer += data


    post_data = buffer.decode('utf-8')
    post_data = post_data.replace('\n', '\\n')
    json_data = json.loads(post_data)

    log.info(json_data)

    ## needs to be commented out because we can't print diacritics in logging (it breaks teh shit)    
    #log.info('[{}]: received print job\n{}'.format(json_data['sn'], json_data['data']))

    printer_id = json_data['sn']
    print_job = json_data['data']
    try:
        if printer_id in printers.keys():
            print_queue = printers[printer_id]['raw']
            id_queue = printers[printer_id]['id_q']
            last_id = get_msgid(print_job)
            log.info('[{}] PRINT JOB : {} QUEUED'.format(printer_id, last_id))

            # Signaling which message is about to be printed
            await id_queue.put(last_id)

            print_task = (print_job_future, print_job.encode('utf-8'))

            # Send message to print queue
            print_queue.put_nowait(print_task)
        else:
            log.error('[PRINT] Failed to print to printer with #SN : {}'.format(printer_id))

    except Exception as e:
        log.error('[PRINT] Failed to decode msg : {}'.format(json_data))
        log.error('[ERROR] {}'.format(e))

    return web.Response(text='.')

async def print_zpl_legacy(request):
    """ This coro receives legacy print job which is base64 encoded 
        Used only by old OrderMan app
    """

    print_job_future = None

    buffer = b""
    async for data, end_of_http_chunk in request.content.iter_chunks():
        buffer += data

    post_data = buffer.decode('utf-8')
    log.debug('POST : {}'.format(post_data))

    for command in str(post_data).split('&'):
        delimiter_pos = str(command).find('=')
        printer = str(command)[:delimiter_pos]
        print_job_encoded = urllib.parse.unquote(str(command)[delimiter_pos+1:])
        printer = str(printer)

        try:
            print_job = base64.b64decode(print_job_encoded)
            log.debug("[{}] Job : {}".format(printer, print_job))
            log.debug("[PRINT] Printers : {}".format(printers))
            if printer in printers.keys():
                print_queue = printers[printer]['raw']
                id_queue = printers[printer]['id_q']
                last_id = get_msgid(print_job)
                log.info('[{}] PRINT JOB : {} QUEUED'.format(printer, last_id))

                # Signaling which message is about to be printed
                await id_queue.put(last_id)
                print_task = (print_job_future, print_job)
                print_queue.put_nowait(print_task)
            else:
                log.error('[PRINT] Failed to print to printer with #SN : {}'.format(printer))
        except Exception as e:
            log.error('[PRINT] Failed to decode msg : {}'.format(print_job_encoded))
            log.error('[ERROR] {}'.format(e))

    return web.Response(text='.')


async def sgd(request):
    """ This coro receives SGD JSON command and sends it to queue feeding
        config websocket of requested printer
    """

    sgd_command_future = asyncio.Future()

    buffer = b""
    async for data, end_of_http_chunk in request.content.iter_chunks():
        buffer += data

    post_data = buffer.decode('utf-8')

    log.debug('POST : {}'.format(post_data))

#
    json_data = json.loads(post_data)
    printer_id = json_data['printer_id']
    sgd_command = str.encode(json_data['sgd_command'])

    try:
        log.info("[SGD] [{}] : {}".format(printer_id, sgd_command))

        if printer_id in printers.keys():
            sgd_queue = printers[printer_id]['config']

            sgd_task = (sgd_command_future, sgd_command)
            log.debug('[SGD] TASK PUT IN QUEUE : {} '.format(sgd_task))
            sgd_queue.put_nowait(sgd_task)
        else:
            log.warning('[SGD] Printer with ID {} has not connected yet'.format(printer_id))
            return web.Response(text='[SGD] Printer with ID {} has not connected yet'.format(printer_id))

    except Exception as e:
        log.error('[SGD] Eception while processing SGD command : {}'.format(sgd_command))
        log.error('[SGD] Eception : {}'.format(e))
        sgd_command_future.cancel()
        return web.Response(text='Config channel disconnected')
#

    while not sgd_command_future.done():
        await asyncio.sleep(0.1)

    return web.Response(text=str(sgd_command_future.result()))


async def ws_disconnect(ws, ws_info):
    """ Disconnect dead websocket client. """
    await ws.close()
    printer_id, channel_name = ws_info.result()
    log.info('[WEBSOCKET KEEPALIVE] closing connection to endpoint {} > channel {}'.format(printer_id, channel_name))


async def ws_keep_alive(ws, ws_info):
    """ Send websocket keepalive packets to clients. """
    while True:
        await asyncio.sleep(options['ws_keepalive_interval'])
        printer_id, channel_name = ws_info.result()
        try:
            log.debug('[WEBSOCKET KEEPALIVE] send PING frame to endpoint {} > channel {}'.format(printer_id, channel_name))
            pong = await ws.ping()
            await asyncio.wait_for(pong, timeout=options['ws_endpoint_timeout'])
        except asyncio.TimeoutError:
            log.debug('[WEBSOCKET KEEPALIVE] missing PONG frame from endpoint {} > channel {}'.format(printer_id, channel_name))
            await ws_disconnect(ws, ws_info)
            await asyncio.sleep(1)


async def reconnect(request):
    """ reconnects raw channel for specified printer if main channel is alive
    """

    buffer = b""
    async for data, end_of_http_chunk in request.content.iter_chunks():
        buffer += data

    post_data = buffer.decode('utf-8')

    log.debug('POST : {}'.format(post_data))

    json_data = json.loads(post_data)
    printer_id = json_data['printer_id']
    channel = json_data['channel']

    if channel not in ('raw', 'config', 'cfg'):
        return web.Response(text=str('Channel not recognized'))

    if printer_id in printers.keys():
        if 'main' in printers[printer_id].keys():
            main_queue = printers[printer_id]['main']
            if channel in 'raw':
                log.info('Trying to open RAW channel per API request')
                main_queue.put_nowait((None, open_raw))
                return web.Response(text=str('Trying to open RAW channel per API request'))
            else:
                log.info('Trying to open CONFIG channel per API request')
                main_queue.put_nowait((None, open_cfg))
                return web.Response(text=str('Trying to open CONFIG channel per API request'))
        else:
            log.warning('Unable to find main channel for printer {}'.format(printer_id))
            return web.Response(text=str('WARNING : Unable to find main channel for printer {}'.format(printer_id)))
    else:
        log.warning('Unable to find active printer {}'.format(printer_id))
        return web.Response(text=str('WARNING : Unable to find active printer {}'.format(printer_id)))


async def handler(websocket, path):
    """ Main handler for websocket connections

        WS(client) --->> WS(server) listener_task(websocket.recv())
        WS(client) <<--- WS(server) producer_task(read from queue, websocket.send())

        has 2 tasks, read from websocket, other to send data via websocket
        - listener_task
            <<-- reads data from websocket
        - producer_task
            -->> ET
            #1 read data from queue*
            #2 send this data via websocket

            * data is fed to this queue usually via AioHTTP API

    """
    log.debug('New websocket connection')

    ws_info = asyncio.Future()
    ws_info.add_done_callback(new_ws_conn)
    queue = asyncio.Queue()
    id_queue = asyncio.Queue()
    sgd_queue = asyncio.Queue()
    future_msg = None
    full_message = None

    # ensure that we ping printer regulary
    ws_keep_alive_task = asyncio.ensure_future(ws_keep_alive(websocket, ws_info))

    while True:
        listener_task = asyncio.ensure_future(websocket.recv())
        producer_task = asyncio.ensure_future(producer(queue))

        try:
            done, pending = await asyncio.wait(
                [listener_task, producer_task],
                return_when=asyncio.FIRST_COMPLETED)

            if listener_task in done:
                # This is message we got from WS
                # message is next parsed in 'consumer' handler
                message = listener_task.result().decode('utf-8')
                # message could be long, we need to put it together before we pass it on to consumer
                # - full_message is msg that can be loaded with json.loads(message)
                full_message, end_of_msg = glue_messages(message, full_message)
                if end_of_msg:
                    log.debug('Message is complete, relaying it to consumer')
                    await consumer(queue, id_queue, full_message, ws_info, sgd_queue)
                    full_message = None
                else:
                    log.debug('Message is incomplete, continuing to read')
                    await asyncio.sleep(0.1)

            else:
                listener_task.cancel()

            if producer_task in done:
                future_msg, message = producer_task.result()
                if future_msg is not None:
                    log.debug('[HANDLER] Producer sent us future, feeding it to queue where future is expected')
                    sgd_queue.put_nowait(future_msg)
                await websocket.send(message)
            else:
                producer_task.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            producer_task.cancel()
            listener_task.cancel()
            ws_keep_alive_task.cancel()
            printer_id, channel_name = ws_info.result()
            log.info('[{}] [{}] Websocket connection terminated : {}'.format(printer_id, channel_name, e))
            break
        except Exception as e:
            producer_task.cancel()
            listener_task.cancel()
            ws_keep_alive_task.cancel()
            printer_id, channel_name = ws_info.result()
            log.error('[{}] [{}] Websocket abnormally terminated : {}'.format(printer_id, channel_name, e))
            print("Exception in user code:")
            print('-'*60)
            traceback.print_exc(file=sys.stdout)
            print('-'*60)
            break

    if future_msg is not None:
        future_msg.cancel()

    printer_id, channel_name = ws_info.result()

    main_queue = None
    if 'main' in printers[printer_id].keys():
        main_queue = printers[printer_id]['main']

    if channel_name == 'CONFIG':
        printers[printer_id].pop('config', None)
        # initiate reconnect of config channel
        if main_queue is not None:
            log.info('[{}] [MAIN] Attempting to re-connect config channel'.format(printer_id))
            main_queue.put_nowait((None, open_cfg))
    elif channel_name == 'RAW':
        printers[printer_id].pop('raw', None)
        if main_queue is not None:
            log.info('[{}] [MAIN] Attempting to re-connect RAW channel'.format(printer_id))
            main_queue.put_nowait((None, open_raw))
    elif channel_name == 'MAIN':
        log.info('[{}] [MAIN] channel is lost, printer should reconnect soon'.format(printer_id))
        # cleanup queues; on reconnect new will be created...
        printers[printer_id].pop('main', None)
        printers[printer_id].pop('id_q', None)

    log.debug('Notifying orderman that we lost websocket connection {} {}'.format(printer_id, channel_name))
    async with aiohttp.request('POST', options['print_job_done'], json={'sn': printer_id, 'job_id': 'disconnected', 'channel': channel_name }) as response:
        log.info('Notify websocket disconnected response : {} - {}'.format(response.status, await response.text()))



def main():
    log.info("Starting aiohttp server : {}:{}".format(options['web_ip'], options['web_port']))
    app = web.Application()
    app.router.add_route('GET', '/list_printers', list_printers)
    app.router.add_route('POST', '/reconnect', reconnect)
    app.router.add_route('POST', '/print_ng', print_zpl_ng)
    app.router.add_route('POST', '/print', print_zpl_legacy)
    app.router.add_route('POST', '/print_ng_dummy', print_zpl_ng_dummy)
    app.router.add_route('POST', '/sgd', sgd)

    log.info("Starting websocket server : {}:{}".format(options['ws_ip'], options['ws_port']))
    loop = asyncio.get_event_loop()

    websocket_server = websockets.serve(handler, options['ws_ip'], options['ws_port'], subprotocols=['v1.weblink.zebra.com'], extra_headers={'Content-Length': '0'})
    loop.run_until_complete(websocket_server)
    loop.run_until_complete(web.run_app(app, host=options['web_ip'], port=options['web_port']))

    ## sigterm
    #loop.run_forever()
    stop = asyncio.Future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
    loop.run_until_complete(stop)

    websocket_server.close()
    loop.run_until_complete(websocket_server.wait_closed())
    log.info("Shutting down websocket server")
    time.sleep(1)


if __name__ == '__main__':
    try:
        with open('zebraman.cfg') as fh:
            options = yaml.load(fh, Loader=yaml.FullLoader)
    except Exception as e:
        print("Config file not present, exiting... {}".format(e))
        sys.exit(1)

    logging.basicConfig(filename=options['log_file'], filemode='a', format='%(asctime)s %(levelname)s [%(module)s:%(lineno)d] %(message)s', level=logging.INFO)
    #write_pid_to_pidfile(options['pid_file'])
    log = logging.getLogger(__name__)
    formatter = logging.Formatter("%(asctime)s %(levelname)s " +
                                  "[%(module)s:%(lineno)d] %(message)s")

    log.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    log.addHandler(ch)

    while True:
        main()
