#!/usr/bin/env python3.5

import asyncio
import logging
import websockets
import base64
import ast
import json

from aiohttp import web

# commands
open_cfg = b'{ "open" : "v1.config.zebra.com" }'
open_raw = b'{ "open" : "v1.raw.zebra.com" }'

print_hello_world = b"""
    ^XA
    ^FT78,76^A0N,28,28^FH\^FDHello\&World^FS
    ^XZ
    """
cmd1 = b'{}{"weblink.ip.conn1.num_connections":null}'

# globals
printers = {}


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


async def consumer(queue, message):
    await asyncio.sleep(1.0)
    log.info('Consumed  {}'.format(message))

    # parse that incoming message
    msg_dict = ast.literal_eval(message.decode("utf-8"))

    if 'discovery_b64' in msg_dict.keys():
        serial_num = getSerialFromDiscovery(message)
        log.info(' *** MAIN channel established *** ')
        log.info("discovered S/N : {}".format(serial_num))
        queue.put_nowait(open_raw)
    
    if 'channel_name' in msg_dict.keys():
        if 'v1.raw.zebra.com' == msg_dict['channel_name']:
            serial_num = msg_dict['unique_id']
            log.info(' *** RAW channel established *** ')
            printers[serial_num] = queue
            #queue.put_nowait(print_hello_world)
            #queue.put_nowait(raw_cmd1)

        elif 'v1.config.zebra.com' == msg_dict['channel_name']:
            serial_num = msg_dict['unique_id']
            log.info(' *** CONFIG channel established *** ')
            queue.put_nowait(cmd1)


async def producer(queue):
    while True:
        try:
            command = queue.get_nowait()
            log.info('Command retreived from queue : {}'.format(command))
            #await asyncio.sleep(1)
            return command

        except asyncio.queues.QueueEmpty:
            #log.info('Queue is empty exception')
            await asyncio.sleep(2)


async def list_printers(request):
    output = json.dumps(list(printers.keys()))
    log.info('OUTPUT JSON : {}'.format(output))
    return web.Response(text=output)


async def zprint(request):
    """ This coro receives print job which is relayed to appropriate queue """
    post_data = request.content.read_nowait()
    log.info('POST : {}'.format(post_data))
    for command in str(post_data).split('&'):
        printer, print_job_encoded = str(command).split('=')
        printer = str(printer)
        print_job = base64.b64decode(print_job_encoded)
        print("Printer : {}".format(printer))
        print("Job : {}".format(print_job))

        #print_queue printers[printer]
        log.info('Printers : {}'.format(printers))
        print_queue = printers['50J161000398']
        print_queue.put_nowait(print_job)
        log.info('Print queue : {}'.format(type(print_queue)))

    return web.Response(text='.')


async def handler(websocket, path):
    log.info('New websocket connection')

    queue = asyncio.Queue()

    while True:
        listener_task = asyncio.ensure_future(websocket.recv())
        producer_task = asyncio.ensure_future(producer(queue))
        done, pending = await asyncio.wait(
            [listener_task, producer_task],
            return_when=asyncio.FIRST_COMPLETED)

        if listener_task in done:
            message = listener_task.result()
            await consumer(queue, message)
        else:
            listener_task.cancel()

        if producer_task in done:
            message = producer_task.result()
            await websocket.send(message)
        else:
            producer_task.cancel()

    
def main():

    log.info("Starting aiohttp server")
    app = web.Application()
    app.router.add_route('GET', '/list_printers', list_printers)
    app.router.add_route('POST', '/zprint', zprint)

    log.info("Starting websocket server!")
    loop = asyncio.get_event_loop()

    start_server = websockets.serve(handler, 'localhost', 6000, subprotocols=['v1.weblink.zebra.com'], extra_headers={'Content-Length': '0'})
    loop.run_until_complete(start_server)
    loop.run_until_complete(web.run_app(app))
    loop.run_forever()
    loop.close()
    log.info("Shutting down websocket server")


if __name__ == '__main__':

    log = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s " +
                                  "[%(module)s:%(lineno)d] %(message)s")

    log.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    log.addHandler(ch)

    main()
