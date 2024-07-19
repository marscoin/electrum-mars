# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import threading
import time
from typing import Optional, Dict, Mapping, Sequence

from . import util
from .bitcoin import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, bh2u, with_lock
from .simple_config import SimpleConfig
from .logging import get_logger, Logger
import logging
from decimal import Decimal, getcontext

try:
    import scrypt
    getPoWHash = lambda x: scrypt.hash(x, x, N=1024, r=1, p=1, buflen=32)
except ImportError:
    util.print_msg("Warning: package scrypt not available; synchronization could be very slow")
    from .scrypt import scrypt_1024_1_1_80 as getPoWHash


_logger = get_logger(__name__)

HEADER_SIZE = 80  # bytes
MAX_TARGET = 0x00000FFFFF000000000000000000000000000000000000000000000000000000
DGW_PAST_BLOCKS = 24
POW_DGW3_HEIGHT = 126000
#POW_TARGET_SPACING = int(2.5 * 60)
POW_TARGET_SPACING = 123
ASERT_HEIGHT = 2999999

class MissingHeader(Exception):
    pass

class InvalidHeader(Exception):
    pass

def serialize_header(header_dict: dict) -> str:
    s = int_to_hex(header_dict['version'], 4) \
        + rev_hex(header_dict['prev_block_hash']) \
        + rev_hex(header_dict['merkle_root']) \
        + int_to_hex(int(header_dict['timestamp']), 4) \
        + int_to_hex(int(header_dict['bits']), 4) \
        + int_to_hex(int(header_dict['nonce']), 4)
    return s

def deserialize_header(s: bytes, height: int) -> dict:
    if not s:
        raise InvalidHeader('Invalid header: {}'.format(s))
    if len(s) != HEADER_SIZE:
        raise InvalidHeader('Invalid header length: {}'.format(len(s)))
    hex_to_int = lambda s: int.from_bytes(s, byteorder='little')
    h = {}
    h['version'] = hex_to_int(s[0:4])
    h['prev_block_hash'] = hash_encode(s[4:36])
    h['merkle_root'] = hash_encode(s[36:68])
    h['timestamp'] = hex_to_int(s[68:72])
    h['bits'] = hex_to_int(s[72:76])
    h['nonce'] = hex_to_int(s[76:80])
    h['block_height'] = height
    return h

def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00'*32
    return hash_raw_header(serialize_header(header))


def hash_raw_header(header: str) -> str:
    return hash_encode(sha256d(bfh(header)))

def pow_hash_header(header):
    return hash_encode(getPoWHash(bfh(serialize_header(header))))


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()  # lock order: take this last; so after Blockchain.lock


def read_blockchains(config: 'SimpleConfig'):
    best_chain = Blockchain(config=config,
                            forkpoint=0,
                            parent=None,
                            forkpoint_hash=constants.net.GENESIS,
                            prev_hash=None)
    blockchains[constants.net.GENESIS] = best_chain
    # consistency checks
    if best_chain.height() > constants.net.max_checkpoint():
        header_after_cp = best_chain.read_header(constants.net.max_checkpoint()+1)
        if not header_after_cp or not best_chain.can_connect(header_after_cp, check_height=False):
            _logger.info("[blockchain] deleting best chain. cannot connect header after last cp to last cp.")
            os.unlink(best_chain.path())
            best_chain.update_size()
    # forks
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        _logger.info(f"[blockchain] deleting chain {filename}: {reason}")
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64-len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64-len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_checkpoint():
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]

# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]


def init_headers_file_for_best_chain():
    b = get_best_chain()
    filename = b.path()
    len_checkpoints = len(constants.net.CHECKPOINTS)
    length = HEADER_SIZE * len(constants.net.CHECKPOINTS) * 2016
    if not os.path.exists(filename) or os.path.getsize(filename) < length:
        with open(filename, 'wb') as f:
            #if length > 0:
            #    f.seek(length - 1)
            #    f.write(b'\x00')
            for i in range(len_checkpoints):
                # this will depend on new checkpoints file
                for height, header_data in b.checkpoints[i][2]:
                    f.seek(height*80)
                    bin_header = util.bfh(header_data)
                    f.write(bin_header)
        util.ensure_sparse_file(filename)
    with b.lock:
        b.update_size()


class Blockchain(Logger):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: SimpleConfig, forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        Logger.__init__(self)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.logger = logging.getLogger("blockchain")
        self.update_size()

    @property
    def checkpoints(self):
        return constants.net.CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        children = self.get_direct_children()
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    def get_direct_children(self) -> Sequence['Blockchain']:
        with blockchains_lock:
            return list(filter(lambda y: y.parent==self, blockchains.values()))

    def get_parent_heights(self) -> Mapping['Blockchain', int]:
        """Returns map: (parent chain -> height of last common block)"""
        with self.lock, blockchains_lock:
            result = {self: self.height()}
            chain = self
            while True:
                parent = chain.parent
                if parent is None: break
                result[parent] = chain.forkpoint - 1
                chain = parent
            return result

    def get_height_of_last_common_block_with_chain(self, other_chain: 'Blockchain') -> int:
        last_common_block_height = 0
        our_parents = self.get_parent_heights()
        their_parents = other_chain.get_parent_heights()
        for chain in our_parents:
            if chain in their_parents:
                h = min(our_parents[chain], their_parents[chain])
                last_common_block_height = max(last_common_block_height, h)
        return last_common_block_height

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False

    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint-1))
        self.assert_headers_file_available(parent.path())
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict. note that in some cases
        # save_header might have already put it there but that's OK
        chain_id = self.get_id()
        with blockchains_lock:
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self) -> None:
        p = self.path()
        self._size = os.path.getsize(p)//HEADER_SIZE if os.path.exists(p) else 0


    @classmethod
    def verify_header(cls, header: dict, prev_hash: str, target: int, expected_header_hash: str=None) -> None:
        logger = logging.getLogger("blockchain")
        logger.info(f"Verifying header at height {header.get('block_height')}:")
        logger.info(f"Header: {header}")
        logger.info(f"Previous hash: {prev_hash}")
        logger.info(f"Calculated target: {target}")
        
        if 'timestamp' not in header:
            logger.error(f"Header missing 'timestamp' key: {header}")
        if 'bits' not in header:
            logger.error(f"Header missing 'bits' key: {header}")
        
        _hash = hash_header(header)
        _powhash = pow_hash_header(header)
        
        if expected_header_hash and expected_header_hash != _hash:
            logger.error(f"Hash mismatch: expected {expected_header_hash}, got {_hash}")
            raise Exception("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
        
        if prev_hash != header.get('prev_block_hash'):
            logger.error(f"Previous hash mismatch: expected {prev_hash}, got {header.get('prev_block_hash')}")
            raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        
        if constants.net.TESTNET:
            return

        bits = cls.target_to_bits(target)
        header_bits = header.get('bits')
        logger.info(f"Calculated bits: {bits}, Header bits: {header_bits}")
        
        # Special handling for the transition block
        if header.get('block_height') == 3000000:
            logger.info("Verifying transition block to ASERT")
            if bits != header_bits:
                logger.warning(f"Transition block bits mismatch: calculated {bits}, header has {header_bits}")
                logger.warning("Allowing mismatch for transition block")
            return

        if bits != header_bits:
            logger.error(f"Bits mismatch: calculated {bits}, header has {header_bits}")
            logger.info(f"Calculated target: {target}")
            logger.info(f"Header target: {cls.bits_to_target(header_bits)}")
            raise Exception(f"bits mismatch: {bits} vs {header_bits}")
        
        block_hash_as_num = int.from_bytes(bfh(_powhash), byteorder='big')
        logger.info(f"Block hash as number: {block_hash_as_num}")
        
        if block_hash_as_num > target:
            logger.error(f"Insufficient proof of work: {block_hash_as_num} > {target}")
            raise Exception(f"insufficient proof of work: {block_hash_as_num} vs target {target}")

        logger.info("Header verification passed")
        
    def verify_chunk(self, index: int, data: bytes) -> None:
        num = len(data) // HEADER_SIZE
        start_height = index * 2016
        prev_hash = self.get_hash(start_height - 1)
        chunk_headers = {'empty': True}

        for i in range(num):
            height = start_height + i
            try:
                expected_header_hash = self.get_hash(height)
            except MissingHeader:
                expected_header_hash = None
            raw_header = data[i*HEADER_SIZE : (i+1)*HEADER_SIZE]
            height = index * 2016 + i
            header = deserialize_header(raw_header, height)
            target = self.get_target(height, chunk_headers)
            self.verify_header(header, prev_hash, target, expected_header_hash)

            chunk_headers[height] = header
            if i == 0:
                chunk_headers['min_height'] = height
                chunk_headers['empty'] = False
            chunk_headers['max_height'] = height
            prev_hash = hash_header(header)

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    @with_lock
    def save_chunk(self, index: int, chunk: bytes):
        assert index >= 0, index
        chunk_within_checkpoint_region = index < len(self.checkpoints)
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(index, chunk)
            return

        delta_height = (index * 2016 - self.forkpoint)
        delta_bytes = delta_height * HEADER_SIZE
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region
        self.write(chunk, delta_bytes, truncate)
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        with self.lock, blockchains_lock:
            # do the swap; possibly multiple ones
            cnt = 0
            while True:
                old_parent = self.parent
                if not self._swap_with_parent():
                    break
                # make sure we are making progress
                cnt += 1
                if cnt > len(blockchains):
                    raise Exception(f'swapping fork with parent too many times: {cnt}')
                # we might have become the parent of some of our former siblings
                for old_sibling in old_parent.get_direct_children():
                    if self.check_hash(old_sibling.forkpoint - 1, old_sibling._prev_hash):
                        old_sibling.parent = self

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False
        if self.parent.get_chainwork() >= self.get_chainwork():
            return False
        self.logger.info(f"swapping {self.forkpoint} {self.parent.forkpoint}")
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        assert forkpoint > parent.forkpoint, (f"forkpoint of parent chain ({parent.forkpoint}) "
                                              f"should be at lower height than children's ({forkpoint})")
        with open(parent.path(), 'rb') as f:
            f.seek((forkpoint - parent.forkpoint)*HEADER_SIZE)
            parent_data = f.read(parent_branch_size*HEADER_SIZE)
        self.write(parent_data, 0)
        parent.write(my_data, (forkpoint - parent.forkpoint)*HEADER_SIZE)
        # swap parameters
        self.parent, parent.parent = parent.parent, self  # type: Optional[Blockchain], Optional[Blockchain]
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(bh2u(parent_data[:HEADER_SIZE]))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        os.replace(child_old_name, parent.path())
        self.update_size()
        parent.update_size()
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool=True) -> None:
        filename = self.path()
        self.assert_headers_file_available(filename)
        with open(filename, 'rb+') as f:
            if truncate and offset != self._size * HEADER_SIZE:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size()

    @with_lock
    def save_header(self, header: dict) -> None:
        delta = header.get('block_height') - self.forkpoint
        data = bfh(serialize_header(header))
        # headers are only _appended_ to the end:
        assert delta == self.size(), (delta, self.size())
        assert len(data) == HEADER_SIZE
        self.write(data, delta*HEADER_SIZE)
        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent.read_header(height)
        if height > self.height():
            return
        delta = height - self.forkpoint
        name = self.path()
        self.assert_headers_file_available(name)
        with open(name, 'rb') as f:
            f.seek(delta * HEADER_SIZE)
            h = f.read(HEADER_SIZE)
            if len(h) < HEADER_SIZE:
                raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        if h == bytes([0])*HEADER_SIZE:
            return None
        return deserialize_header(h, height)

    def header_at_tip(self) -> Optional[dict]:
        """Return latest header."""
        height = self.height()
        return self.read_header(height)

    def is_tip_stale(self) -> bool:
        STALE_DELAY = 8 * 60 * 60  # in seconds
        header = self.header_at_tip()
        if not header:
            return True
        # note: We check the timestamp only in the latest header.
        #       The Bitcoin consensus has a lot of leeway here:
        #       - needs to be greater than the median of the timestamps of the past 11 blocks, and
        #       - up to at most 2 hours into the future compared to local clock
        #       so there is ~2 hours of leeway in either direction
        if header['timestamp'] + STALE_DELAY < time.time():
            return True
        return False

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= constants.net.max_checkpoint()
            at_chunk_boundary = (height+1) % 2016 == 0
            return within_cp_range and at_chunk_boundary

        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 2016
            #h, t, _ = self.checkpoints[index]
            h, t, extra_headers = self.checkpoints[index]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    def get_timestamp(self, height):
        if height < len(self.checkpoints) * 2016 and (height+1) % 2016 == 0:
            index = height // 2016
            _, _, ts = self.checkpoints[index]
            return ts
        return self.read_header(height).get('timestamp')

    def get_target(self, height, chunk_headers=None):
        if chunk_headers is None:
            chunk_headers = {'empty': True}

        if height >= POW_DGW3_HEIGHT and height < ASERT_HEIGHT:
            return self.get_target_dgw_v3(height, chunk_headers)
        elif height >= ASERT_HEIGHT:
            prev_header = self.read_header(height - 1)
            if prev_header is None:
                raise MissingHeader(f"Previous header not found at height {height - 1}")
            return self.get_target_asert(height, chunk_headers)
        else:
            return MAX_TARGET

    def get_target_dgw_v3(self, height: int, chunk_headers: Optional[dict]) -> int:
        if chunk_headers['empty']:
            chunk_empty = True
        else:
            chunk_empty = False
            min_height = chunk_headers['min_height']
            max_height = chunk_headers['max_height']

        count_blocks = 1
        while count_blocks <= DGW_PAST_BLOCKS:
            reading_h = height - count_blocks
            reading_header = self.read_header(reading_h)
            if not reading_header and not chunk_empty and min_height <= reading_h <= max_height:
                reading_header = chunk_headers[reading_h]
            if not reading_header:
                raise MissingHeader()
            reading_time = reading_header.get('timestamp')
            reading_target = self.bits_to_target(reading_header.get('bits'))

            if count_blocks == 1:
                past_target_avg = reading_target
                last_time = reading_time
            past_target_avg = (past_target_avg * count_blocks + reading_target) // (count_blocks + 1)

            count_blocks += 1

        new_target = past_target_avg
        actual_timespan = last_time - reading_time
        target_timespan = DGW_PAST_BLOCKS * POW_TARGET_SPACING

        if actual_timespan < target_timespan // 3:
            actual_timespan = target_timespan // 3
        if actual_timespan > target_timespan * 3:
            actual_timespan = target_timespan * 3

        new_target *= actual_timespan
        new_target //= target_timespan

        if new_target > MAX_TARGET:
            return MAX_TARGET

        new_target = self.bits_to_target(self.target_to_bits(new_target))
        return new_target
    

    def get_target_asert(self, height: int, chunk_headers: Optional[dict]=None) -> int:
        # Constants
        HALF_LIFE = 2 * 3600  # 2 hours in seconds
        TARGET_SPACING = 123  # 2 Mars-minutes
        ANCHOR_HEIGHT = 2999999  # Fixed anchor block height

        self.logger.info(f"ASERT calculation for height {height}")

        if chunk_headers is None:
            chunk_headers = {'empty': True}
        
        chunk_empty = chunk_headers.get('empty', True)
        if not chunk_empty:
            min_height = chunk_headers['min_height']
            max_height = chunk_headers['max_height']

        # Fetch anchor header
        anchor_header = self.read_header(ANCHOR_HEIGHT)
        if not anchor_header and not chunk_empty and min_height <= ANCHOR_HEIGHT <= max_height:
            anchor_header = chunk_headers[ANCHOR_HEIGHT]
        if not anchor_header:
            self.logger.error(f"ASERT Error: Anchor block header not found at height {ANCHOR_HEIGHT}")
            raise MissingHeader("Anchor block header not found")

        # Fetch previous header
        prev_height = height - 1
        prev_header = self.read_header(prev_height)
        if not prev_header and not chunk_empty and min_height <= prev_height <= max_height:
            prev_header = chunk_headers[prev_height]
        if not prev_header:
            self.logger.error(f"ASERT Error: Previous block header not found at height {prev_height}")
            raise MissingHeader(f"Previous block header not found at height {prev_height}")

        self.logger.info(f"Anchor header: {anchor_header}")
        self.logger.info(f"Previous header: {prev_header}")

        anchor_target = self.bits_to_target3(anchor_header['bits'])
        time_diff = prev_header['timestamp'] - anchor_header['timestamp']
        height_diff = height - ANCHOR_HEIGHT - 1

        self.logger.info(f"anchor_target={anchor_target}, time_diff={time_diff}, height_diff={height_diff}")

        # Calculate exponent
        exponent = ((time_diff - TARGET_SPACING * height_diff) * 65536) // HALF_LIFE
        self.logger.info(f"exponent={exponent}")

        # Calculate target
        if exponent < 0:
            target = anchor_target >> (-exponent // 65536)
        else:
            target = anchor_target << (exponent // 65536)

        self.logger.info(f"target after shift={target}")

        # Apply the fractional part of the exponent
        frac = exponent & 0xFFFF
        factor = 65536 + ((frac * 195766423245049 + frac * frac * 971821376 + frac * frac * frac * 5127) >> 48)
        target = (target * factor) >> 16

        self.logger.info(f"final target={target}")

        if target > MAX_TARGET:
            self.logger.info(f"target exceeds MAX_TARGET, using MAX_TARGET")
            return MAX_TARGET

        calculated_bits = self.target_to_bits3(target)
        self.logger.info(f"calculated bits={calculated_bits}")

        return calculated_bits  # Return bits instead of target



    #Bitcoin Cash
    # def get_target_asert(self, height: int, chunk_headers: Optional[Dict[int, dict]]=None) -> int:
    #     # Constants from the original ASERT algorithm
    #     HALF_LIFE = 2 * 3600  # 2 hours in seconds (original value)
    #     TARGET_SPACING = 123  # 2 Mars-minutes (original value)
    #     ANCHOR_HEIGHT = 2999999  # Fixed anchor block height
        
    #     # Additional constants from Bitcoin Cash implementation
    #     RBITS = 16
    #     RADIX = 65536
    #     MAX_BITS = 0x1d00ffff
    #     MAX_TARGET = self.bits_to_target3(MAX_BITS)

    #     self.logger.info(f"ASERT Debug: Constants - HALF_LIFE={HALF_LIFE}, TARGET_SPACING={TARGET_SPACING}, ANCHOR_HEIGHT={ANCHOR_HEIGHT}")

    #     if chunk_headers is None:
    #         chunk_headers = {'empty': True}
        
    #     chunk_empty = chunk_headers.get('empty', True)
    #     min_height = chunk_headers.get('min_height', 0)
    #     max_height = chunk_headers.get('max_height', 0)

    #     # Fetch anchor header
    #     anchor_header = self.read_header(ANCHOR_HEIGHT)
    #     if not anchor_header and not chunk_empty and min_height <= ANCHOR_HEIGHT <= max_height:
    #         anchor_header = chunk_headers[ANCHOR_HEIGHT]
    #     if not anchor_header:
    #         self.logger.error(f"ASERT Error: Anchor block header not found at height {ANCHOR_HEIGHT}")
    #         raise MissingHeader("Anchor block header not found")

    #     # Fetch previous header
    #     prev_height = height - 1
    #     prev_header = self.read_header(prev_height)
    #     if not prev_header and not chunk_empty and min_height <= prev_height <= max_height:
    #         prev_header = chunk_headers[prev_height]
    #     if not prev_header:
    #         self.logger.error(f"ASERT Error: Previous block header not found at height {prev_height}")
    #         raise MissingHeader(f"Previous block header not found at height {prev_height}")

    #     # Check for required keys
    #     required_keys = ['timestamp', 'bits']
    #     for header in (anchor_header, prev_header):
    #         for key in required_keys:
    #             if key not in header:
    #                 self.logger.error(f"ASERT Error: Required '{key}' key is missing in the block header")
    #                 raise KeyError(f"Required '{key}' key is missing in the block header a:'{anchor_header}' p:'{prev_header}'")

    #     anchor_bits = anchor_header['bits']
    #     time_diff = prev_header['timestamp'] - anchor_header['timestamp']
    #     height_diff = height - ANCHOR_HEIGHT - 1

    #     self.logger.debug(f"ASERT Debug: anchor_bits={anchor_bits}, time_diff={time_diff}, height_diff={height_diff}")

    #     # Convert anchor_bits to target
    #     anchor_target = self.bits_to_target3(anchor_bits)

    #     # Calculate exponent using floor division
    #     exponent = ((time_diff - TARGET_SPACING * (height_diff + 1)) * RADIX) // HALF_LIFE

    #     self.logger.debug(f"ASERT Debug: exponent={exponent}")

    #     # Shift exponent into the (0, 1] interval
    #     shifts = exponent >> RBITS
    #     exponent -= shifts * RADIX

    #     # Compute approximated target * 2^(fractional part) * 65536
    #     target = anchor_target * (RADIX + ((195766423245049 * exponent + 971821376 * exponent**2 + 5127 * exponent**3 + 2**47) >> (RBITS*3)))

    #     self.logger.debug(f"ASERT Debug: target after fractional calculation={target}")

    #     # Shift to multiply by 2^(integer part)
    #     if shifts < 0:
    #         target >>= -shifts
    #     else:
    #         target <<= shifts

    #     # Remove the 65536 multiplier
    #     target >>= RBITS

    #     self.logger.debug(f"ASERT Debug: final target={target}")

    #     if target == 0:
    #         self.logger.debug("ASERT Debug: target is 0, returning bits for target 1")
    #         return self.target_to_bits3(1)
    #     if target > MAX_TARGET:
    #         self.logger.debug(f"ASERT Debug: target exceeds MAX_TARGET, using MAX_BITS")
    #         return MAX_BITS

    #     calculated_bits = self.target_to_bits3(target)
    #     self.logger.debug(f"ASERT Debug: calculated bits={calculated_bits}")

    #     return calculated_bits

    @classmethod
    def bits_to_target3(self, bits: int) -> int:
        size = bits >> 24
        word = bits & 0x00ffffff
        if size <= 3:
            return word >> (8 * (3 - size))
        else:
            return word << (8 * (size - 3))

    @classmethod
    def target_to_bits3(self, target: int) -> int:
        if target == 0:
            return 0
        size = (target.bit_length() + 7) // 8
        mask64 = 0xffffffffffffffff
        if size <= 3:
            compact = (target & mask64) << (8 * (3 - size))
        else:
            compact = (target >> (8 * (size - 3))) & mask64

        if compact & 0x00800000:
            compact >>= 8
            size += 1

        return compact | size << 24


    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        bitsN = (bits >> 24) & 0xff
        if not (0x03 <= bitsN <= 0x1e):
            raise Exception("First part of bits should be in [0x03, 0x1e]")
        bitsBase = bits & 0xffffff
        if not (0x8000 <= bitsBase <= 0x7fffff):
            raise Exception("Second part of bits should be in [0x8000, 0x7fffff]")
        return bitsBase << (8 * (bitsN-3))

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        c = ("%064x" % target)[2:]
        while c[:2] == '00' and len(c) > 6:
            c = c[2:]
        bitsN, bitsBase = len(c) // 2, int(c[:6], 16)
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase


    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        bitsN = (bits >> 24) & 0xff
        if not (0x03 <= bitsN <= 0x1e):
            raise Exception("First part of bits should be in [0x03, 0x1e]")
        bitsBase = bits & 0xffffff
        if not (0x8000 <= bitsBase <= 0x7fffff):
            raise Exception("Second part of bits should be in [0x8000, 0x7fffff]")
        return bitsBase << (8 * (bitsN-3))

    
    @classmethod
    def bits_to_target2(self, bits):
        return self.set_compact(bits)
    
    @classmethod    
    def target_to_bits2(self, target):
        return self.get_compact(target)
    

    @staticmethod
    def set_compact(nCompact):
        nSize = nCompact >> 24
        nWord = nCompact & 0x007fffff
        if nSize <= 3:
            value = nWord >> (8 * (3 - nSize))
        else:
            value = nWord << (8 * (nSize - 3))
        return Decimal(value)

    @staticmethod
    def get_compact(value):
        # Ensure the context is sufficient to avoid overflow errors
        getcontext().prec = 28  # Adjust precision if necessary

        # Convert Decimal to an integer (rounding as needed)
        if isinstance(value, Decimal):
            # Adjust the scaling factor based on the decimal's precision or a fixed scale
            scale_factor = 10 ** value.as_tuple().exponent.abs()  # Adjust if necessary
            int_value = int(value * scale_factor)
        else:
            int_value = int(value)

        # Now we can safely convert to bytes
        bytes_repr = int_value.to_bytes((int_value.bit_length() + 7) // 8, 'big')
        nSize = len(bytes_repr)
        if nSize == 0:
            return 0
        elif nSize <= 3:
            nCompact = int.from_bytes(bytes_repr, 'big') << (8 * (3 - nSize))
        else:
            nCompact = int.from_bytes(bytes_repr[:3], 'big') << 8 * (nSize - 3)

        nCompact |= nSize << 24
        if bytes_repr[0] & 0x80:
            nCompact |= 0x00800000
        return nCompact



    @classmethod
    def target_to_bits(cls, target: int) -> int:
        c = ("%064x" % target)[2:]
        while c[:2] == '00' and len(c) > 6:
            c = c[2:]
        bitsN, bitsBase = len(c) // 2, int.from_bytes(bfh(c[:6]), byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def chainwork_of_header_at_height(self, height: int) -> int:
        """work done by single header at given height"""
        chunk_idx = height // 2016 - 1
        #chunk_idx = height

        target = self.get_target(chunk_idx)
        work = ((2 ** 256 - target - 1) // (target + 1)) + 1
        return work

    @with_lock
    def get_chainwork(self, height=None) -> int:
        if height is None:
            height = max(0, self.height())
        if constants.net.TESTNET:
            # On testnet/regtest, difficulty works somewhat different.
            # It's out of scope to properly implement that.
            return height
        last_retarget = height // 2016 * 2016 - 1
        cached_height = last_retarget
        while _CHAINWORK_CACHE.get(self.get_hash(cached_height)) is None:
            if cached_height <= -1:
                break
            cached_height -= 2016
        assert cached_height >= -1, cached_height
        running_total = _CHAINWORK_CACHE[self.get_hash(cached_height)]
        while cached_height < last_retarget:
            cached_height += 2016
            work_in_single_header = self.chainwork_of_header_at_height(cached_height)
            work_in_chunk = 2016 * work_in_single_header
            running_total += work_in_chunk
            _CHAINWORK_CACHE[self.get_hash(cached_height)] = running_total
        cached_height += 2016
        work_in_single_header = self.chainwork_of_header_at_height(cached_height)
        work_in_last_partial_chunk = (height % 2016 + 1) * work_in_single_header
        return running_total + work_in_last_partial_chunk

    def can_connect(self, header: dict, check_height: bool=True) -> bool:
        if header is None:
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            return False
        if height == 0:
            return hash_header(header) == constants.net.GENESIS
        try:
            prev_hash = self.get_hash(height - 1)
        except Exception as e:
            print(e)
            return False
        if prev_hash != header.get('prev_block_hash'):
            return False
        try:
            target = self.get_target(height)
        except MissingHeader:
            return False
        try:
            self.verify_header(header, prev_hash, target)
        except BaseException as e:
            return False
        return True

    def connect_chunk(self, idx: int, hexdata: str) -> bool:
        assert idx >= 0, idx
        try:
            data = bfh(hexdata)
            self.verify_chunk(idx, data)
            self.save_chunk(idx, data)
            return True
        except BaseException as e:
            self.logger.info(f'verify_chunk idx {idx} failed: {repr(e)}')
            return False

    def get_checkpoints(self):
        # for each chunk, store the hash of the last block and the target after the chunk
        cp = []
        n = self.height() // 2016
        for index in range(n):
            height = (index + 1) * 2016 -1
            h = self.get_hash(height)
            target = self.get_target(height)
            if len(h.strip('0')) == 0:
                raise Exception('%s file has not enough data.' % self.path())
            dgw3_headers = []
            if os.path.exists(self.path()):
                with open(self.path(), 'rb') as f:
                    lower_header = height - DGW_PAST_BLOCKS
                    for height in range(height, lower_header-1, -1):
                        f.seek(height*80)
                        hd = f.read(80)
                        if len(hd) < 80:
                            raise Exception('Expected to read a full header.' 'This was only {} bytes'.format(len(hd)))
                        dgw3_headers.append((height, bh2u(hd)))
            cp.append((h, target, dgw3_headers))
        return cp


def check_header(header: dict) -> Optional[Blockchain]:
    """Returns any Blockchain that contains header, or None."""
    if type(header) is not dict:
        return None
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    """Returns the Blockchain that has a tip that directly links up
    with header, or None.
    """
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None


def get_chains_that_contain_header(height: int, header_hash: str) -> Sequence[Blockchain]:
    """Returns a list of Blockchains that contain header, best chain first."""
    with blockchains_lock: chains = list(blockchains.values())
    chains = [chain for chain in chains
              if chain.check_hash(height=height, header_hash=header_hash)]
    chains = sorted(chains, key=lambda x: x.get_chainwork(), reverse=True)
    return chains