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
cmd1 = b'{}{"weblink.ip.conn1.num_connections":null}'

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


async def consumer(queue, id_queue, message, printer_id, channel_name):
    await asyncio.sleep(0.1)
    log.info('Consumed  {}'.format(message))

    # parse that incoming message
    msg_dict = json.loads(message.decode('utf-8'))

    if 'discovery_b64' in msg_dict.keys():
        serial_num = getSerialFromDiscovery(message)
        log.info(' *** MAIN channel established *** ')
        log.info("discovered S/N : {}".format(serial_num))
        queue.put_nowait(open_raw)
        printer_id.set_result(serial_num)
        channel_name.set_result('MAIN')

    if 'channel_name' in msg_dict.keys():
        if 'v1.raw.zebra.com' == msg_dict['channel_name']:
            serial_num = msg_dict['unique_id']
            log.info(' *** RAW channel established *** ')
            printers[serial_num] = {}
            printers[serial_num]['raw'] = queue
            printers[serial_num]['id_q'] = id_queue
            # Signaling IDs
            queue.put_nowait(print_spojeno)
            await id_queue.put('connected')
            printer_id.set_result(serial_num)
            channel_name.set_result('RAW')

        elif 'v1.config.zebra.com' == msg_dict['channel_name']:
            serial_num = msg_dict['unique_id']
            log.info(' *** CONFIG channel established *** ')
            printers[serial_num] = {}
            printers[serial_num]['config'] = queue
            queue.put_nowait(cmd1)
            printer_id.set_result(serial_num)
            channel_name.set_result('CONFIG')


    # parse alert messages
    if 'alert' in msg_dict.keys():
        log.debug('ALERT PARSING')
        if 'condition' in msg_dict['alert'].keys():
            # print job done message
            if msg_dict['alert']['condition'] == 'PQ JOB COMPLETED':
                log.debug('ALERT PARSING #PQ JOB COMPLETE')
                printer_id = msg_dict['alert']['unique_id']
                # Signaling IDs
                id_queue = printers[printer_id]['id_q']
                try:
                    last_id = await id_queue.get()
                except asyncio.queues.QueueEmpty:
                    log.error('ID Queue was empty, this is not supposed to happen')
                    last_id = 'ERROR'

                log.info('Printer {} printed a job : {}'.format(printer_id, last_id))
                # make get request to url
                response = await get(options['print_job_done'] + printer_id + '&job_id=' + last_id, compress=True) 
                try:
                    log.info("PQ JOB ACK RESPONSE  : {}".format(response))
                except:
                    log.error('Unable to read response #1')

            # get data scanned from barcode
            if msg_dict['alert']['condition'] == 'SGD SET':
                log.debug('ALERT PARSING #SGD SET')
                if 'setting_value' in msg_dict['alert'].keys():
                    scanned_data = msg_dict['alert']['setting_value']
                    printer_id = msg_dict['alert']['unique_id']

                    log.info('Printer {} scanned data : {} '.format(printer_id, scanned_data))

                    response = await post(options['scan_data_url'] + printer_id, data=json.dumps({'barcode' : scanned_data})) 
                    try:
                        log.info("SCAN DATA RELAY RESPONSE FOR DATA {} : {}".format(scanned_data, response))
                    except:
                        log.error('Unable to read response #2')


async def producer(queue):
    while True:
        try:
            command = queue.get_nowait()
            log.info('Command retreived from queue : {}'.format(command))
            return command

        except asyncio.queues.QueueEmpty:
            #log.info('Queue is empty exception')
            await asyncio.sleep(0.1)


async def list_printers(request):
    output = json.dumps(list(printers.keys()))
    log.info('OUTPUT JSON : {}'.format(output))
    return web.Response(text=output)


async def zpl64_print(request):
    """ This coro receives print job which is relayed to appropriate queue
    """

    post_data = request.content.read_nowait().decode('utf-8')
    log.info('POST : {}'.format(post_data))
    for command in str(post_data).split('&'):

        #
        delimiter_pos = str(command).find('=')
        printer = str(command)[:delimiter_pos]
        print_job_encoded = urllib.parse.unquote(str(command)[delimiter_pos+1:])
        printer = str(printer)

        #
        try:
            print_job = base64.b64decode(print_job_encoded)
            log.info("[PRINT] Job : {}".format(print_job))
            log.info("[PRINT] Printers : {}".format(printers))
            if printer in printers.keys():
                print_queue = printers[printer]['raw']
                id_queue = printers[printer]['id_q']
                last_id = get_msgid(print_job)

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

    post_data = request.content.read_nowait()
    log.info('POST : {}'.format(post_data))

    for command in str(post_data).split('&'):
        #
        delimiter_pos = str(command).find('=')
        printer = str(command)[:delimiter_pos]
        print_job_encoded = urllib.parse.unquote(str(command)[delimiter_pos+1:])
        printer = str(printer)

        #
        try:
            print_job = base64.b64decode(print_job_encoded)
            log.info("[SGD] Job : {}".format(print_job))
            log.info("[SGD] Printer : {}".format(printer))
            log.info("[SGD] Printers : {}".format(printers))

            if printer in printers.keys():
                print_queue = printers[printer]['config']
                print_queue.put_nowait(print_job)
            else:
                log.error('[SGD] Command failed on printer with #SN : {}'.format(printer))

            log.info('[SGD] Print queue : {}'.format(type(print_queue)))
        except:
            log.error('[SGD] Failed to decode msg : {}'.format(print_job_encoded))

    return web.Response(text='.')


async def handler(websocket, path):
    log.info('New websocket connection')

    printer_id = asyncio.Future()
    channel_name = asyncio.Future()
    queue = asyncio.Queue()
    id_queue = asyncio.Queue()

    while True:
        listener_task = asyncio.ensure_future(websocket.recv())
        producer_task = asyncio.ensure_future(producer(queue))

        try:
            done, pending = await asyncio.wait(
                [listener_task, producer_task],
                return_when=asyncio.FIRST_COMPLETED)

            if listener_task in done:
                message = listener_task.result()
                await consumer(queue, id_queue, message, printer_id, channel_name)
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
            log.info('[{}] [{}] Websocket connection terminated : {}'.format(printer_id.result(), channel_name.result(), e))
            break
        except Exception as e:
            producer_task.cancel()
            listener_task.cancel()
            log.error('[{}] [{}] Websocket abnormally terminated : {}'.format(printer_id.result(), channel_name.result(), e))
            break

    log.debug('Notifying orderman that we lost websocket connection {} {}'.format(printer_id.result(), channel_name.result()))
    response = await get(options['print_job_done'] + printer_id.result() + '&job_id=disconnected&channel=' + channel_name.result(), compress=True)


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
        log.info('Message contains ID : {}'.format(last_id))
        return last_id
    else:
        log.warn('No ID found in message : {}'.format(message))
        return 'custom'


def main():

    log.info("Starting aiohttp server")
    app = web.Application()
    app.router.add_route('GET', '/list_printers', list_printers)
    app.router.add_route('POST', '/print', zpl64_print)
    app.router.add_route('POST', '/sgd', sgd)

    log.info("Starting websocket server!")
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
    log = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s " +
                                  "[%(module)s:%(lineno)d] %(message)s")

    log.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    log.addHandler(ch)

    main()
