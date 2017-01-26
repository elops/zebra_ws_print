#!/usr/bin/env python3.5

import asyncio
import logging
import websockets
import base64
import ast
import json
import aiohttp
import urllib
import requests
import yaml
import sys
import re

from aiohttp import web

# commands
open_cfg = b'{ "open" : "v1.config.zebra.com" }'
open_raw = b'{ "open" : "v1.raw.zebra.com" }'

print_spojeno = b"""
    ^XA
    ^LL 200
    ^FT78,76^A0N,28,28^FH\^FD INTERNET CONNECTION : OK ^FS
    ^XZ
    """

# globals
printers = {}
options = {}


def getSerialFromDiscovery(packet):
    """ receives bytes e.g.
        b'{\r\n  "discovery_b64" : "OiwuBAIBAAFaQlIAAFpENDIwLWNoZWxsbwAAAAAAAAAA...
        returns string e.g.
        50J161000398
    """

    # convert packet to dictionary
    packet_dict = ast.literal_eval(packet.decode("utf-8"))
    if 'discovery_b64' in packet_dict.keys():
        return base64.b64decode(packet_dict['discovery_b64'])[188:188+12].decode('utf-8')
    else:
        return None


async def get(*args, **kwargs):
    response = await aiohttp.request('GET', *args, **kwargs)
    return (await response.text())


async def post(*args, **kwargs):
    response = await aiohttp.request('POST', *args, **kwargs)
    return (await response.text())


async def consumer(queue, id_queue, message, ws_info, future_msg):
    await asyncio.sleep(0.1)
    log.debug('Consumed  {}'.format(message))

    # parse that incoming message
    msg_dict = json.loads(message.decode('utf-8'))

    # read data from msg_dict
    if 'unique_id' in msg_dict.keys():
        serial_num = msg_dict['unique_id']
        log.debug('[{}] identified sn from message'.format(serial_num))
    elif 'discovery_b64' in msg_dict.keys():
        log.debug('[PRINTER RESPONSE] : \n{}'.format(message.decode('utf-8')))
    elif 'alert' in msg_dict.keys():
        log.debug('[PRINTER RESPONSE] : \n{}'.format(message.decode('utf-8')))
    else:
        log.info('[PRINTER RESPONSE] : \n{}'.format(message.decode('utf-8')))


    # Handle discovery message which establishes the main websocket channel
    if 'discovery_b64' in msg_dict.keys():
        serial_num = getSerialFromDiscovery(message)
        # Set future to help identify WS conn
        ws_info.set_result((serial_num,'MAIN'))

        log.debug(' *** MAIN channel established with : {} *** '.format(serial_num))
        queue.put_nowait(open_raw)
        queue.put_nowait(open_cfg)
        # initialize global dictionary for this printer SN
        printers[serial_num] = {}


    # Handle establish messages for RAW and CONFIG channel
    if 'channel_name' in msg_dict.keys():
        if 'v1.raw.zebra.com' == msg_dict['channel_name']:
            # Set future to help identify WS conn
            ws_info.set_result( (serial_num, 'RAW') )

            log.debug(' *** RAW channel established with : {} *** '.format(serial_num))
            printers[serial_num]['raw'] = queue
            printers[serial_num]['id_q'] = id_queue
            # Print connected message, and feed 'id_queue' which print job was submitted
            queue.put_nowait(print_spojeno)
            await id_queue.put('connected')

        elif 'v1.config.zebra.com' == msg_dict['channel_name']:
            # Set future to help identify WS conn
            ws_info.set_result( (serial_num, 'CONFIG') )

            log.debug(' *** CONFIG channel established with : {} *** '.format(serial_num))
            printers[serial_num]['config'] = queue
            await get(options['print_job_done'] + serial_num + '&job_id=connected&channel=CONFIG', compress=True)


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

                log.info('[{}] PRINT JOB DONE : {}'.format(printer_sn, last_id))
                # make get request to url
                response = await get(options['print_job_done'] + printer_sn + '&job_id=' + last_id + '&channel=RAW', compress=True)
                try:
                    log.info("[{}] PQ JOB ACK RESPONSE  : {}".format(printer_sn, response))
                except:
                    log.error('Unable to read response #1')

            # get data scanned from barcode
            if msg_dict['alert']['condition'] == 'SGD SET':
                log.debug('ALERT PARSING #SGD SET')
                if 'setting_value' in msg_dict['alert'].keys():
                    scanned_data = msg_dict['alert']['setting_value']
                    printer_sn = msg_dict['alert']['unique_id']

                    log.info('[{}] SCANNED DATA : {} '.format(printer_sn, scanned_data))

                    response = await post(options['scan_data_url'] + printer_sn, data=json.dumps({'barcode' : scanned_data}))
                    try:
                        log.info("[{}] SCANNED DATA : {} ACK {}".format(printer_sn, scanned_data, response))
                    except:
                        log.error('Unable to read response #2')


async def producer(queue):
    while True:
        try:
            command = queue.get_nowait()
            log.debug('Command retreived from queue : {}'.format(command))
            return command

        except asyncio.queues.QueueEmpty:
            log.debug('Queue is empty exception')
            await asyncio.sleep(0.1)


async def list_printers(request):
    output = json.dumps(list(printers.keys()))
    log.info('OUTPUT JSON : {}'.format(output))
    return web.Response(text=output)


async def zpl64_print(request):
    """ This coro receives print job which is relayed to appropriate queue
    """

    post_data = request.content.read_nowait().decode('utf-8')
    log.debug('POST : {}'.format(post_data))
    for command in str(post_data).split('&'):

        #
        delimiter_pos = str(command).find('=')
        printer = str(command)[:delimiter_pos]
        print_job_encoded = urllib.parse.unquote(str(command)[delimiter_pos+1:])
        printer = str(printer)

        #
        try:
            print_job = base64.b64decode(print_job_encoded)
            log.debug("[{}] Job : {}".format(printer, print_job))
            log.debug("[PRINT] Printers : {}".format(printers))
            if printer in printers.keys():
                print_queue = printers[printer]['raw']
                id_queue = printers[printer]['id_q']
                last_id = get_msgid(print_job)
                log.info('[{}] PRINT JOB QUEUED : {}'.format(printer, last_id))

                # Signaling which message is about to be printed
                await id_queue.put(last_id)

                # Send message to print queue
                print_queue.put_nowait(print_job)
            else:
                log.error('[PRINT] Failed to print to printer with #SN : {}'.format(printer))

        except:
            log.error('[PRINT] Failed to decode msg : {}'.format(print_job_encoded))

    return web.Response(text='.')


async def sgd(request):
    """ This coro receives SGD JSON command and sends it to queue feeding
        config websocket of requested printer
    """

    post_data = request.content.read_nowait().decode('utf-8')
    log.debug('POST : {}'.format(post_data))

    for command in str(post_data).split('&'):
        #
        delimiter_pos = str(command).find('=')
        printer = str(command)[:delimiter_pos]
        task_b64_encoded = urllib.parse.unquote(str(command)[delimiter_pos+1:])
        printer = str(printer)

        #
        try:
            sgd_command = base64.b64decode(task_b64_encoded)
            log.info("[SGD] [{}] : {}".format(printer, sgd_command))

            if printer in printers.keys():
                sgd_queue = printers[printer]['config']
                sgd_queue.put_nowait(sgd_command)
            else:
                log.error('[SGD] Command failed on printer with #SN : {}'.format(printer))

            log.debug('[SGD] Queue : {}'.format(type(sgd_queue)))
        except:
            log.error('[SGD] Failed to decode msg : {}'.format(task_b64_encoded))

    return web.Response(text='.')


async def handler(websocket, path):
    log.debug('New websocket connection')

    ws_info = asyncio.Future()
    ws_info.add_done_callback(new_ws_conn)
    queue = asyncio.Queue()
    id_queue = asyncio.Queue()
    future_msg = None

    while True:
        listener_task = asyncio.ensure_future(websocket.recv())
        producer_task = asyncio.ensure_future(producer(queue))

        try:
            done, pending = await asyncio.wait(
                [listener_task, producer_task],
                return_when=asyncio.FIRST_COMPLETED)

            if listener_task in done:
                message = listener_task.result()
                await consumer(queue, id_queue, message, ws_info, future_msg)
            else:
                listener_task.cancel()

            if producer_task in done:
                message = producer_task.result()
                await websocket.send(message)
            else:
                producer_task.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            producer_task.cancel()
            listener_task.cancel()
            printer_id, channel_name = ws_info.result()
            log.info('[{}] [{}] Websocket connection terminated : {}'.format(printer_id, channel_name, e))
            break
        except Exception as e:
            producer_task.cancel()
            listener_task.cancel()
            printer_id, channel_name = ws_info.result()
            log.error('[{}] [{}] Websocket abnormally terminated : {}'.format(printer_id, channel_name, e))
            break

    printer_id, channel_name = ws_info.result()
    log.debug('Notifying orderman that we lost websocket connection {} {}'.format(printer_id, channel_name))
    response = await get(options['print_job_done'] + printer_id + '&job_id=disconnected&channel=' + channel_name, compress=True)


def get_msgid(message):
    """ This function searches string to find MSG UUID or some other indicator
        which would identify type of msg
        input is binary msg e.g.
        b'foo bar baz\n in few lines\n\n ^FX UUID: #1234 test\n'
        output is string which is returned
    """

    matches = re.findall(r'\^FX UUID: #(\d+)', message.decode('utf-8'))
    if len(matches):
        last_id = matches[0]
        log.debug('Message contains ID : {}'.format(last_id))
        return last_id
    else:
        log.warn('No ID found in message : {}'.format(message))
        return 'custom'

def new_ws_conn(future):
    """ Prints msg from future """
    printer_id, channel_name = future.result()
    log.info('[{}] [{}] New websocket connection'.format(printer_id, channel_name))


def main():

    log.info("Starting aiohttp server : {}:{}".format(options['web_ip'], options['web_port']))
    app = web.Application()
    app.router.add_route('GET', '/list_printers', list_printers)
    app.router.add_route('POST', '/print', zpl64_print)
    app.router.add_route('POST', '/sgd', sgd)

    log.info("Starting websocket server : {}:{}".format(options['ws_ip'], options['ws_port']))
    loop = asyncio.get_event_loop()

    start_server = websockets.serve(handler, options['ws_ip'], options['ws_port'], subprotocols=['v1.weblink.zebra.com'], extra_headers={'Content-Length': '0'})
    loop.run_until_complete(start_server)
    loop.run_until_complete(web.run_app(app, host=options['web_ip'], port=options['web_port']))
    loop.run_forever()
    loop.close()
    log.info("Shutting down websocket server")


if __name__ == '__main__':

    try:
        options = yaml.load(open('zebraman.cfg'))
    except Exception as e:
        print("Config file not present, exiting...")
        sys.exit(1)

    logging.basicConfig(filename=options['log_file'], filemode='w', format='%(asctime)s %(levelname)s [%(module)s:%(lineno)d] %(message)s', level=logging.INFO)
    log = logging.getLogger(__name__)
    formatter = logging.Formatter("%(asctime)s %(levelname)s " +
                                  "[%(module)s:%(lineno)d] %(message)s")

    log.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    log.addHandler(ch)

    main()
