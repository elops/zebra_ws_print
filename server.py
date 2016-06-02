#!/usr/bin/env python3.5

import asyncio
import asyncio_redis
import logging
import websockets
import base64
import ast
import sys

# commands
open_cfg = b'{ "open" : "v1.config.zebra.com" }'
open_raw = b'{ "open" : "v1.raw.zebra.com" }'
print_hello_world = b"""
    ^XA
    ^FT78,76^A0N,28,28^FH\^FDHello\&World^FS
    ^XZ
    """
cmd1 = b'{}{"weblink.ip.conn1.num_connections":null}'

raw_cmd1 = b"""
    ^XA

    ^FO140,15
    ^A0,40,40
    ^FD
    Some text!
    ^FS

    ^FO140,60
    ^A0,40,40
    ^FD
    From some ZPL tut
    ^FS

    ^FO140,105
    ^A0,40,40
    ^FD
    hrvoje.spoljar@gmail.com
    ^FS

    ^FO140,150^BY3
    ^BMN,B,100,Y,N,N
    ^FD123456^FS

    ^XZ
    """



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
        log.info("Addint task to queue : {}".format(open_raw))
        queue.put_nowait(open_raw)
    
    if 'channel_name' in msg_dict.keys():
        if 'v1.raw.zebra.com' == msg_dict['channel_name']:
            serial_num = msg_dict['unique_id']
            log.info(' *** RAW channel established *** ')
            queue.put_nowait(print_hello_world)
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
            #log.info('WS recv <<<< {} : {}'.format(websocket, message))
            await consumer(queue, message)
        else:
            listener_task.cancel()

        if producer_task in done:
            message = producer_task.result()
            #log.info('WS send >>>> {} : {}'.format(websocket, message))
            await websocket.send(message)
        else:
            producer_task.cancel()

    
def main():
    log.info("Starting websocket server!")

    loop = asyncio.get_event_loop()

    start_server = websockets.serve(handler, 'localhost', 6000, subprotocols=['v1.weblink.zebra.com'], extra_headers={'Content-Length': '0'})
    loop.run_until_complete(start_server)
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
