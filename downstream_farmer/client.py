#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function

import binascii
import hashlib
import json
import threading
import time
import logging

import requests
import heartbeat
from datetime import datetime, timedelta

from .utils import handle_json_response, ThreadPool, sizeof_fmt, \
    BurstQueue, Counter
from .exc import DownstreamError
from .contract import DownstreamContract

heartbeat_types = {'Swizzle': heartbeat.Swizzle.Swizzle,
                   'Merkle': heartbeat.Merkle.Merkle}

api_prefix = '/api/downstream/v1'


class DownstreamClient(object):

    def __init__(self,
                 url,
                 token,
                 address,
                 size,
                 msg,
                 sig,
                 manager,
                 chunk_dir):
        self.server = url.strip('/')
        self.api_url = self.server + api_prefix
        self.token = token
        self.address = address
        self.desired_size = size
        self.msg = msg
        self.sig = sig
        self.heartbeat = None

        self.contract_thread = None
        self.heartbeat_thread = None
        self.worker_pool = None
        self.cert_path = None
        self.verify_cert = True
        self.running = True
        self.thread_manager = manager
        self.chunk_dir = chunk_dir
        self._set_requests_verify_arg()

        self.contracts_lock = threading.RLock()
        self.contracts = dict()
        self.heartbeat_count_lock = threading.Lock()
        self.heartbeat_count = 0
        self.desired_heartbeats = None
        # response margin defaults to 20.  contracts will begin to be answered
        # at least this number of seconds before they expire
        self.response_margin = 20
        # update margin defaults to 20.  contracts will begin to be updated
        # no later than this amount of time after it is possible to update them
        # the margin between then a contract is updated and when it must be
        # proven will be dependent on the interval.  if the interval is less
        # than the update_margin, the contract will fail.
        self.update_margin = 20
        # status of the contract, used for determining whether to update or
        # what
        self.contract_status = dict()
        self.contract_status_lock = threading.Lock()
        # this is the initial estimated onboard rate, that determines how fast
        # the farmer can onboard contracts in bytes/second
        # this provides an initial estimate.... it will be dependent on disk
        # and or download speeds in actual applications
        self.estimated_onboard_speed = 2000000
        self.estimated_contract_interval = 60

        self.submission_queue = BurstQueue()
        self.update_queue = BurstQueue()

        self.proving_counter = Counter()
        self.submitting_counter = Counter()
        self.updating_counter = Counter()

        self.logger = logging.getLogger(
            'storj.downstream_farmer.DownstreamClient')
        self.start = None

    def set_cert_path(self, cert_path):
        """Sets the path of a CA-Bundle to use for verifying requests
        """
        self.cert_path = cert_path
        self._set_requests_verify_arg()

    def set_verify_cert(self, verify_cert):
        """Sets whether or not to verify the ssl certificate
        """
        self.verify_cert = verify_cert
        self._set_requests_verify_arg()

    def _set_requests_verify_arg(self):
        """Sets the appropriate requests verify argument
        """
        if (self.verify_cert):
            self.requests_verify_arg = self.cert_path
        else:
            self.requests_verify_arg = False

    def connect(self):
        """Connects to a downstream-node server.
        """
        if (self.token is None):
            if (self.address is None):
                raise DownstreamError(
                    'If no token is specified, address must be.')
            # get a new token
            url = '{0}/new/{1}'.\
                format(self.api_url, self.address)
            # if we have a message/signature to send, send it
            if (self.msg != '' and self.sig != ''):
                data = {
                    "message": self.msg,
                    "signature": self.sig
                }
                headers = {
                    'Content-Type': 'application/json'
                }
                resp = requests.post(
                    url,
                    data=json.dumps(data),
                    headers=headers,
                    verify=self.requests_verify_arg)
            else:
                # otherwise, just normal request
                resp = requests.get(url, verify=self.requests_verify_arg)
        else:
            # try to use our token
            url = '{0}/heartbeat/{1}'.\
                format(self.api_url, self.token)

            resp = requests.get(url, verify=self.requests_verify_arg)

        try:
            r_json = handle_json_response(resp)
        except DownstreamError as ex:
            raise DownstreamError('Unable to connect: {0}'.
                                  format(str(ex)))

        for k in ['token', 'heartbeat', 'type']:
            if (k not in r_json):
                raise DownstreamError('Malformed response from server.')

        if r_json['type'] not in heartbeat_types.keys():
            raise DownstreamError('Unknown Heartbeat Type')

        self.token = r_json['token']
        self.heartbeat \
            = heartbeat_types[r_json['type']].fromdict(r_json['heartbeat'])

        # we can calculate farmer id for display...
        token = binascii.unhexlify(self.token)
        token_hash = hashlib.sha256(token).hexdigest()[:20]
        self.logger.info('Confirmed token: {0}'.format(self.token))
        self.thread_manager.stats.set('token', self.token)
        self.logger.info('Farmer id: {0}'.format(token_hash))

    def _get_contracts(self, size=None):
        """Gets chunk contracts from the connected node

        :param size: the maximum size of the contract
        :returns: a list of obtained contracts
        """
        url = '{0}/chunk/{1}'.format(self.api_url, self.token)
        if (size is not None):
            url += '/{0}'.format(size)

        resp = requests.get(url, verify=self.requests_verify_arg)

        try:
            r_json = handle_json_response(resp)
        except DownstreamError as ex:
            # can't handle an invalid token
            raise DownstreamError('Unable to get token: {0}'.
                                  format(str(ex)))

        if ('chunks' not in r_json or not isinstance(r_json['chunks'], list)):
            raise DownstreamError('Malformed response from server.')

        contracts = list()

        for chunk in r_json['chunks']:

            for k in ['file_hash', 'seed', 'size', 'challenge', 'tag', 'due']:
                if (k not in chunk):
                    self.logger.warn('Malformed chunk sent from server.')
                    continue

            contract = DownstreamContract(
                self,
                chunk['file_hash'],
                chunk['seed'],
                chunk['size'],
                self.heartbeat.challenge_type().fromdict(chunk['challenge']),
                datetime.utcnow() + timedelta(seconds=int(chunk['due'])),
                self.heartbeat.tag_type().fromdict(chunk['tag']),
                self.thread_manager,
                self.chunk_dir)

            contracts.append(contract)

        return contracts

    def get_total_size(self):
        with self.contracts_lock:
            if (len(self.contracts) > 0):
                return sum([c.size for c in self.contracts.values()])
            else:
                return 0

    def contract_count(self):
        with self.contracts_lock:
            return len(self.contracts)

    def uptime(self):
        if (self.start is not None):
            return datetime.utcnow() - self.start
        else:
            return timedelta()

    def _add_contract(self, contract):
        with self.contracts_lock:
            contract.generate_data()
            self.contracts[contract.hash] = contract

    def _remove_all_contracts(self):
        to_remove = list()
        with self.contracts_lock:
            for c in self.contracts.values():
                to_remove.append(c)
        for c in to_remove:
            self._remove_contract(c)

    def _remove_contract(self, contract):
        with self.contracts_lock:
            if (contract.hash in self.contracts):
                contract.cleanup_data()
                del self.contracts[contract.hash]

    def _remove_contract_by_hash(self, contract_hash):
        with self.contracts_lock:
            if (contract_hash in self.contracts):
                self._remove_contract(self.contracts[contract_hash])

    def _get_average_chunk_generation_rate(self):
        with self.contracts_lock:
            if (len(self.contracts) > 0):
                total = sum(
                    [c.chunk_generation_rate for c in self.contracts.values()])
            else:
                total = 0

            if (total > 0):
                return float(total) / float(len(self.contracts))
            else:
                return self.estimated_onboard_speed

    def _get_average_contract_interval(self):
        with self.contracts_lock:
            if (len(self.contracts) > 0):
                total = sum([c.estimated_interval.total_seconds()
                             for c in self.contracts.values()])
                return float(total) / float(len(self.contracts))
            else:
                return self.estimated_contract_interval

    def _size_to_fill(self):
        """Returns the size to request this round from verification node
        Estimates disk write speed and average contract interval to
        yield the correct size for the next batch
        """
        total_margin = self.update_margin
        desired_write_time = self._get_average_contract_interval() - \
            total_margin
        average_gen_rate = self._get_average_chunk_generation_rate()
        # half the size just to account for
        # other overhead in producing challenges
        # etc.
        max_obtainable_size = int(average_gen_rate * desired_write_time * 0.5)
        size_needed = self.desired_size - self.get_total_size()
        if (size_needed <= max_obtainable_size):
            return size_needed
        else:
            return max_obtainable_size

    def _update_contract_stats(self):
        total_size = self.get_total_size()
        contracts = self.contract_count()
        fill_ratio = '{0} / {1}'.format(sizeof_fmt(total_size),
                                        sizeof_fmt(self.desired_size))
        fraction = float(total_size) / float(self.desired_size)
        fill_percent = '{0}%'.format(round(fraction * 100.0, 3))
        self.thread_manager.stats.set(
            'filled', '{0} ({1})'.format(fill_ratio, fill_percent))
        self.thread_manager.stats.set('contracts', contracts)
        self.thread_manager.stats.set('space_bar', fraction)
        self.logger.info('Contracts: {0}, Total size: {1}'.
                         format(contracts, fill_ratio))
        self.logger.info('Capacity filled {0}'.format(fill_percent))

    def _run_contract_manager(self, retry=False):
        """This loop will maintain the desired total contract size, if
        possible
        """
        online_already = False

        while (self.thread_manager.running):
            size_to_fill = self._size_to_fill()
            while (self.thread_manager.running and size_to_fill > 0):
                self.logger.info('Requesting chunks to fill {0}'
                                 .format(sizeof_fmt(size_to_fill)))
                try:
                    contracts = self._get_contracts(size_to_fill)
                except DownstreamError as ex:
                    if (retry):
                        self.logger.error('Get contracts failed: {0}, retrying'
                                          .format(str(ex)))
                        continue
                    else:
                        raise
                obtained_size = sum([c.size for c in contracts])
                if (obtained_size > size_to_fill):
                    raise DownstreamError('Server sent too much chunk data,'
                                          'size exceeded. Rejecting data.')
                self.logger.info('Obtained {0} contracts for a total size of'
                                 ' {1}'.format(len(contracts),
                                               sizeof_fmt(obtained_size)))
                if (len(contracts) > 0):
                    for contract in contracts:
                        self._add_contract(contract)
                        self._update_contract_stats()
                        # and begin proving this contract
                        self._prove_async(contract)
                        if (not self.thread_manager.running):
                            break
                else:
                    self.logger.info(
                        'There were no chunks available on the server.')
                    break

                size_to_fill = self._size_to_fill()

            if (not self.thread_manager.running):
                # we already exited.  contract_manager needs to return now
                break
            # wait until we need to obtain a new contract
            if (not online_already):
                online_already = True
            self.contract_thread.wait(30)

            if (self.desired_heartbeats is not None
                    and self.heartbeat_count >= self.desired_heartbeats):
                # signal a shutdown, and return
                print('Heartbeat number requirement met.')
                self.logger.info('Heartbeat number requirement met.')
                self.thread_manager.signal_shutdown()
                break

        # contract manager is done, remove all contracts
        self._remove_all_contracts()

    def _prove_async(self, contract):
        # print('Scheduling proof for contract {0}.'.format(contract))
        self.worker_pool.put_work(self._prove, (contract, ))

    def _prove(self, contract):
        """Calculates a proof for the specified contract and puts it into the
        proof queue
        """
        try:
            with self.proving_counter():
                proven = contract.update_proof()
        except DownstreamError as ex:
            self.logger.warn('Unable to fulfill contract: {0}'.format(str(ex)))
            self._remove_contract(contract)
            return

        if (proven):
            submission_time = (contract.expiration
                               - timedelta(seconds=self.response_margin))
            # print('Putting {0} into submission queue.'.format(contract))
            self.submission_queue.put(contract, submission_time)
        else:
            self.logger.warn('Proof for contract {0} was not available.'
                             .format(contract))

        self.heartbeat_thread.wake()

    def _submit_async(self, contracts):
        self.worker_pool.put_work(self._submit, (contracts, ))

    def _submit(self, contracts):
        """Submits the specified contracts
        """
        with self.submitting_counter(len(contracts)):
            start = time.clock()

            url = '{0}/answer/{1}'.format(self.api_url,
                                          self.token)

            proofs = [c.proof_data for c in contracts]
            contract_dict = {c.hash: c for c in contracts}

            data = {
                'proofs': proofs
            }
            headers = {
                'Content-Type': 'application/json'
            }

            try:
                resp = requests.post(url,
                                     data=json.dumps(data),
                                     headers=headers,
                                     verify=self.requests_verify_arg)
            except:
                raise DownstreamError('Unable to perform HTTP post.')

            try:
                r_json = handle_json_response(resp)
            except DownstreamError as ex:
                raise DownstreamError(
                    'Challenge answer failed: {0}'.format(str(ex)))

            if ('report' not in r_json or not isinstance(r_json['report'],
                                                         list)):
                raise DownstreamError('Malformed response from server.')

            submitted = set()

            for contract_report in r_json['report']:
                if ('file_hash' not in contract_report
                        or contract_report['file_hash'] not in contract_dict):
                    # fail nicely with a malformed contract report
                    self.logger.warn('Unexpected contract report.')
                    continue

                contract = contract_dict[contract_report['file_hash']]

                if ('error' in contract_report):
                    self.logger.error('Error answering challenge for contract'
                                      ' {0}: {1}, '
                                      .format(contract,
                                              contract_report['error']))
                    continue
                if ('status' not in contract_report
                        or contract_report['status'] != 'ok'):
                    self.logger.error('No status for contract {0}'
                                      .format(contract))
                    continue

                # everything seems to be in order for this contract
                # and now, once a new challenge is obtained, this contract can
                # be answered again
                contract.answered = True
                submitted.add(contract)
                with self.heartbeat_count_lock:
                    self.heartbeat_count += 1
                    self.thread_manager.stats.set(
                        'heartbeats', self.heartbeat_count)
                    if (self.desired_heartbeats is not None):
                        self.contract_thread.wake()

            stop = time.clock()
            self.logger.info('Submitted {0} proofs successfully in {1} seconds'
                             .format(len(submitted), round(stop - start, 3)))

        # place submitted proofs in update queue
        for c in contracts:
            if (c not in submitted):
                self.logger.error('Contract {0} not successfully submitted, '
                                  'dropping'.format(c))
                self._remove_contract(c)
            else:
                # print('Putting {0} into update queue.'.format(c))
                ready_time = c.expiration
                update_time = (c.expiration
                               + timedelta(seconds=self.update_margin))
                self.update_queue.put(c, update_time, ready_time)

        # and wake heartbeat manager again
        self.heartbeat_thread.wake()

    def _update_async(self, contracts):
        self.worker_pool.put_work(self._update, (contracts, ))

    def _update(self, contracts):
        with self.updating_counter(len(contracts)):
            start = time.clock()

            hashes = [c.hash for c in contracts]

            url = '{0}/challenge/{1}'.format(self.api_url,
                                             self.token)

            data = {
                'hashes': hashes
            }
            headers = {
                'Content-Type': 'application/json'
            }
            try:
                resp = requests.post(url,
                                     data=json.dumps(data),
                                     headers=headers,
                                     verify=self.requests_verify_arg)
            except:
                raise DownstreamError('Unable to perform HTTP post.')

            try:
                r_json = handle_json_response(resp)
            except DownstreamError:
                raise DownstreamError('Challenge update failed.')

            if 'challenges' not in r_json:
                raise DownstreamError('Malformed response from server.')

            # challenges is in r_json
            challenges = r_json['challenges']
            updated = set()

            for challenge in challenges:
                if ('file_hash' not in challenge):
                    raise DownstreamError('Malformed response from server.')

                try:
                    contract = self.contracts[challenge['file_hash']]
                except KeyError:
                    self.logger.warn('Unexpected challenge update.')
                    continue

                if ('error' in challenge or 'status' in challenge):
                    if 'error' in challenge:
                        message = challenge['error']
                    else:
                        message = challenge['status']
                    self.logger.error('Couldn\'t update contract {0}: {1}'
                                      .format(contract, message))
                    continue

                for k in ['challenge', 'due', 'answered']:
                    if (k not in challenge):
                        self.logger.error(
                            'Malformed challenge for contract {0}'
                            .format(contract))
                        continue

                contract.challenge = self.heartbeat.challenge_type().\
                    fromdict(challenge['challenge'])
                contract.expiration = datetime.utcnow()\
                    + timedelta(seconds=int(challenge['due']))
                contract.answered = challenge['answered']

                updated.add(contract)

            stop = time.clock()
            self.logger.info('Updated {0} contracts in {1} seconds'
                             .format(len(updated), round(stop - start, 3)))

        for c in contracts:
            if (c not in updated):
                self.logger.error('Contract {0} not updated, dropping'
                                  .format(c))
                self._remove_contract(c)
            else:
                self._prove_async(c)

        # dont need to wake heartbeat manager because we didn't put anything
        # into a queue

    def _run_heartbeat_manager(self):
        while self.thread_manager.running:
            # we are managing two BurstQueues.
            # when a contract is obtained or updated, it is proven
            # after being proven, it is placed into the submission queue
            # after being submitted, it is put into the update queue
            # after being updated, it is proven again and the cycle repeats
            contracts_to_submit = self.submission_queue.get()

            if (len(contracts_to_submit) > 0):
                self.logger.info(
                    'Submitting {0} contracts'
                    .format(len(contracts_to_submit)))
                self._submit_async(contracts_to_submit)

            contracts_to_update = self.update_queue.get()

            if (len(contracts_to_update) > 0):
                self.logger.info(
                    'Updating {0} contracts'.format(len(contracts_to_update)))
                self._update_async(contracts_to_update)

            self.thread_manager.stats.set(
                'updating', self.updating_counter.count)
            self.thread_manager.stats.set(
                'submitting', self.submitting_counter.count)
            self.thread_manager.stats.set(
                'proving', self.proving_counter.count)

            worker_load = '{0}%'.format(
                round(self.worker_pool.calculate_loading() * 100.0, 3))
            worker_count = self.worker_pool.thread_count()
            max_load = '{0}%'.format(
                round(self.worker_pool.max_load() * 100.0, 3))
            self.thread_manager.stats.set('worker_threads', worker_count)
            self.thread_manager.stats.set('avg_load', worker_load)
            self.thread_manager.stats.set('max_load', max_load)

            next = [self.submission_queue.next_due(),
                    self.update_queue.next_due()]

            next = [n for n in next if n is not None]

            if (len(next) > 0):
                seconds_to_sleep = (
                    min(next) - datetime.utcnow()).total_seconds()
            else:
                seconds_to_sleep = None

            self.thread_manager.sleep(seconds_to_sleep)

    def run_async(self, retry=False, number=None):
        """Starts the contract management loop

        :param retry: whether to retry on obtaining a contract upon failure
        :param number: the number of challenges to answer
        """
        self.heartbeat_count = 0
        self.desired_heartbeats = number
        self.start = datetime.utcnow()

        # create the contract manager
        self.contract_thread = self.thread_manager.create_thread(
            name='ContractThread',
            target=self._run_contract_manager,
            args=(retry,))

        # create the heartbeat manager
        self.heartbeat_thread = self.thread_manager.create_thread(
            name='HeartbeatThread',
            target=self._run_heartbeat_manager)

        # create the thread pool for challenges
        self.worker_pool = ThreadPool(self.thread_manager, 1)

        self.worker_pool.start()
        self.heartbeat_thread.start()
        self.contract_thread.start()
