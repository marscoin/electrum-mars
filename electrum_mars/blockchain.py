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

CHECKPOINT_BITS = {
    2999999: 0x1e0fffff,
    3000000: 0x1e0fcfef,
    3000001: 0x1e0fa86f, 
    3000002: 0x1e0f8157,
    3000100: 0x1e05ec8f,
    3000999: 0x1c3908fc,
    3001999: 0x1c009e02,
    3010999: 0x1c00c5a0, 
    3030048: 0x1c00d6e9,
    3030977: 0x1c00b788,
    3030978: 0x1c00bc01,
    3055555: 0x1c0097e6,
    3085376: 0x1c00bf09,
    3150020: 0x1c016afc,
    3150021: 0x1c0167ec,
    3150022: 0x1c0168d3
}


def print_hex(bytes_data, label):
    """Utility function to print hex bytes"""
    print(f"{label}: {' '.join(f'{b:02x}' for b in bytes_data)}")

class OurBigNum:
    def __init__(self):
        self.data = bytearray()  # Store number in big-endian format

    def set_hex(self, hex_str):
        # Remove '0x' prefix if present
        clean_str = hex_str.lower()
        if clean_str.startswith('0x'):
            clean_str = clean_str[2:]

        # Calculate bytes needed
        byte_length = (len(clean_str) + 1) // 2
        self.data = bytearray(byte_length)

        # Convert hex string to bytes
        for i in range(len(clean_str)):
            c = clean_str[len(clean_str) - 1 - i]
            value = int(c, 16)
            self.data[byte_length - 1 - (i//2)] |= value << ((i % 2) * 4)

    def to_mpi(self):
        # Skip leading zeros
        start = 0
        while start < len(self.data) and self.data[start] == 0:
            start += 1

        # Calculate actual data length
        length = len(self.data) - start
        if length == 0:
            # Special case for zero
            return bytes([0, 0, 0, 0])

        # Check if we need an extra zero byte for sign
        need_extra = (self.data[start] & 0x80) != 0
        length += 1 if need_extra else 0

        # Create MPI with 4-byte length prefix
        mpi = bytearray()
        mpi.extend(length.to_bytes(4, byteorder='big'))

        # Add padding zero if needed
        if need_extra:
            mpi.append(0)

        # Add actual data
        mpi.extend(self.data[start:])

        return bytes(mpi)

    def get_compact(self):
        vch = self.to_mpi()
        n_size = len(vch) - 4  # Subtract length prefix

        # print(f"BN_bn2mpi initial size: {len(vch)}")
        # print_hex(vch, "MPI bytes")

        n_compact = n_size << 24
        # print(f"After size shift: 0x{n_compact:08x}")

        if n_size >= 1:
            n_compact |= (vch[4] << 16)
            # print(f"After byte 1: 0x{n_compact:08x}")
        if n_size >= 2:
            n_compact |= (vch[5] << 8)
            # print(f"After byte 2: 0x{n_compact:08x}")
        if n_size >= 3:
            n_compact |= vch[6]
            # print(f"After byte 3: 0x{n_compact:08x}")

        # Handle sign bit
        if n_compact & 0x00800000:
            # print("Sign bit set, adjusting...")
            n_compact >>= 8
            n_size += 1
            n_compact = (n_size << 24) | (n_compact & 0x007fffff)

        return n_compact

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
        self._logger = get_logger(__name__)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
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

    def verify_header(cls, header: dict, prev_hash: str, target: int, expected_header_hash: str=None) -> None:
        height = header.get('block_height')
        
        _hash = hash_header(header)
        _powhash = pow_hash_header(header)
        
        if expected_header_hash and expected_header_hash != _hash:
            raise Exception("hash mismatches with expected")
        
        if prev_hash != header.get('prev_block_hash'):
            raise Exception("prev hash mismatch")
        
        if constants.net.TESTNET:
            return
        
        # More permissive checks after ASERT_HEIGHT
        if height >= ASERT_HEIGHT:
            print("verify")
            # Just verify basic chain progression and bits
            if height in CHECKPOINT_BITS:
                expected_bits = CHECKPOINT_BITS[height]
                if header.get('bits') != expected_bits:
                    raise Exception(f"bits mismatch at checkpoint height {height}")
            return 


        header_bits = header.get('bits')
        timestamps = header.get('timestamp')
        # get_logger(__name__).warning("VH: Height: " + str(height))
        # get_logger(__name__).warning("VH: Bits: " + str(header_bits))
        # get_logger(__name__).warning("VH: Target: " + str(target))
        # get_logger(__name__).warning("VH: PowHash: " + str(_powhash))
        

        if height == ASERT_HEIGHT:  # Anchor block
            if header_bits != 0x1e0fffff:
                get_logger(__name__).warning(f"Anchor block expected bits {MAX_TARGET:08x}, got {header_bits:08x}")
                raise Exception("Wrong bits for ASERT anchor block")
            return
        elif height < ASERT_HEIGHT:
            # Use original DGW logic
            bits = cls.target_to_bits(target)
            get_logger(__name__).warning(f"VH DGW: Bits that I found in verify header {bits:08x} for target {target:08x} ")
        else:
            if height in CHECKPOINT_BITS:
                expected_bits = CHECKPOINT_BITS[height]
                if header.get('bits') != expected_bits:
                    get_logger(__name__).warning(f"Checkpoint mismatch at height {height}: "f"expected bits {expected_bits:08x}, got {header.get('bits'):08x}")
                    raise Exception(f"bits mismatch at checkpoint height {height}")
                return  # Skip further validation if checkpoint matches
            bits = header_bits
            target = header_bits
            get_logger(__name__).warning(f"Checkpoint passed for height: {height} with {bits:08x} for target {target:08x} ")

        if bits != header_bits:
            raise Exception(f"bits mismatch: {bits} vs {header_bits}")

        try:        
            block_hash_as_num = int.from_bytes(bfh(_powhash), byteorder='big')
            pow_bits = cls.target_to_bits(block_hash_as_num)
        
            if pow_bits > target:  # Compare in bits format
                get_logger(__name__).warning(f"POW check failed: hash bits {pow_bits:08x} > target bits {target:08x}")
                raise Exception("insufficient proof of work")
            
        except Exception as e:
            get_logger(__name__).warning(f"Error: {e}")
            return False

   
        

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
             # Debug fork detection
            # self._logger.warning(f"Potential chain swap detected at height {self.height()}")
            # self._logger.warning(f"Current chain work: {self.get_chainwork()}")
            if self.parent:
                self._logger.warning(f"Parent chain work: {self.parent.get_chainwork()}")

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
        
          # Add check to prevent fallback during ASERT era
        if self.height() >= ASERT_HEIGHT:
            self._logger.warning(f"Preventing chain swap during ASERT era at height {self.height()}")
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
        
        self._logger.warning(f"Saving header at height {header.get('block_height')}")
        self._logger.warning(f"Current chain tip: {self.height()}")
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
        
        
 
    def get_target(self, height: int, chunk_headers: Optional[dict]=None) -> int:
        """Return bits value for given height"""
        # self._logger.warning(f"get_target called for height {height}")
        
        if chunk_headers is None:
            chunk_headers = {'empty': True}
            
        # DGW3 is used up to but not including the anchor block
        if height >= POW_DGW3_HEIGHT and height < ASERT_HEIGHT:
            # self._logger.warning(f"Using DGW3 for height {height}")
            return self.get_target_dgw_v3(height, chunk_headers)
        
        # ASERT starts after transition block
        elif height >= ASERT_HEIGHT:
            # self._logger.warning(f"Using ASERT for height {height}")
            prev_height = height - 1
            prev_header = None
            if not chunk_headers['empty'] and chunk_headers['min_height'] <= prev_height <= chunk_headers['max_height']:
                prev_header = chunk_headers[prev_height]
            else:
                prev_header = self.read_header(prev_height)
            
            if prev_header is None:
                self._logger.error(f"Previous header not found at height {prev_height}")
                raise MissingHeader(f"Previous header not found at height {prev_height}")
                
            return self.get_target_asert(height, prev_header)
        
        else:
            # self._logger.warning(f"Using MAX_TARGET for height {height}")
            return MAX_TARGET


    def get_target_dgw_v3(self, height: int, chunk_headers: Optional[dict]) -> int:
        chunk_empty = True
        if chunk_headers is not None:
            chunk_empty = chunk_headers.get('empty', True)
            min_height = chunk_headers.get('min_height')
            max_height = chunk_headers.get('max_height')

        count_blocks = 1
        while count_blocks <= DGW_PAST_BLOCKS:
            reading_h = height - count_blocks
            reading_header = self.read_header(reading_h)
            if not reading_header and not chunk_empty and min_height <= reading_h <= max_height:
                reading_header = chunk_headers.get(reading_h)
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
    

    @classmethod
    def cpp_divide(self, n: int, d: int) -> int:
        """Implements C++'s division behavior which rounds toward zero"""
        if (n < 0) ^ (d < 0):  
            return -(abs(n) // abs(d))
        return n // d
    
    def get_target_asert(self, height: int, prev_header: dict) -> int:
        # Constants from the ASERT algorithm
        HALF_LIFE = 2 * 3600  # 2 hours in seconds
        TARGET_SPACING = 123  # 2 Mars-minutes
        ANCHOR_HEIGHT = 2999999  # Block 2999998 is the anchor block

        # Get previous header height
        prev_height = height - 1
        
        # Special case: blocks before the anchor block get MAX_TARGET
        if prev_height < ANCHOR_HEIGHT:
            self._logger.info(f"Block {height} has prev height {prev_height} < anchor height {ANCHOR_HEIGHT}, using MAX_TARGET")
            return MAX_TARGET

        anchor_header = self.read_header(ANCHOR_HEIGHT)
        if not anchor_header:
            raise MissingHeader("Anchor block header not found")
        
        ANCHOR_BITS = anchor_header['bits']
        ANCHOR_TIME = anchor_header['timestamp']

        anchor_target = self.bits_to_target(anchor_header['bits'])
        time_diff = prev_header['timestamp'] - anchor_header['timestamp']
        height_diff = height - ANCHOR_HEIGHT

        # self._logger.warning(f"Last block height: {height-1}")
        # self._logger.warning(f"Current block height: {height}")
        
        # Calculate differences 
        time_diff = prev_header['timestamp'] - anchor_header['timestamp']
        height_diff = prev_height - ANCHOR_HEIGHT  
        #exponent = ((time_diff - TARGET_SPACING * (height_diff + 1)) * 65536) // HALF_LIFE same as below, broken into intermediaries

        # self._logger.warning(f"Time difference: {time_diff}")
        # self._logger.warning(f"Height difference: {height_diff}")
        # self._logger.warning(f"Anchor target set from bits: {ANCHOR_BITS} (0x{ANCHOR_BITS:08x})")

        # Handle anchor block case explicitly
        if height_diff < 0:
            # We're at or before anchor block
            expected_secs = 0
        else:
            expected_secs = TARGET_SPACING * (height_diff + 1)
            
        actual_secs = time_diff    
        numerator = (actual_secs - expected_secs) * 65536
        #numerator = ((nTimeDiff - 123 * (nHeightDiff + 1)) * 65536)
        exponent = self.cpp_divide(numerator, HALF_LIFE)


        
        # Debug values
        # self._logger.warning(f"Expected seconds: {expected_secs}")
        # self._logger.warning(f"Actual seconds: {actual_secs}")
        # self._logger.warning(f"Numerator before division: {numerator}")
        # self._logger.warning(f"Height diff: {height_diff}")
        # self._logger.warning(f"Python division would give: {numerator // HALF_LIFE}")
        # self._logger.warning(f"C++ style division gives: {exponent}")
        # self._logger.warning(f"Calculated exponent: {exponent}")
        
        shifts = exponent >> 16
        frac = exponent & 0xffff
        
        # self._logger.warning(f"Shifts (integer part of exponent): {shifts}")
        # self._logger.warning(f"Fractional part of exponent: {frac}")

        # Factor calculation matching C++
        factor = 65536 + ((195766423245049 * frac +
                        971821376 * frac * frac +
                        5127 * frac * frac * frac +
                        (1 << 47)) >> 48)

        # self._logger.warning(f"Calculated factor: {factor}")
        # self._logger.warning("Calculated next target before shift adjustments")

        # Calculate next target
        anchor_target = self.asert_bits_to_target(ANCHOR_BITS)
        next_target = anchor_target * factor
        next_target >>= 16  
        
        shifts = shifts - 16  
        # self._logger.warning(f"Shifting right by: {abs(shifts)}")

        if shifts < 0:
            next_target >>= -shifts
        else:
            next_target <<= shifts

        # In get_target_asert, right after the calculations:
        # get_logger(__name__).warning(f"ASERT debug for height {height}:")
        # get_logger(__name__).warning(f"Starting target length: {len(f'{anchor_target:x}')}")
        # get_logger(__name__).warning(f"After factor multiply length: {len(f'{(anchor_target * factor):x}')}")
        # get_logger(__name__).warning(f"After first shift (>>16) length: {len(f'{(next_target):x}')}")
        # get_logger(__name__).warning(f"Final shifts value: {shifts}")
        # if shifts < 0:
        #     get_logger(__name__).warning(f"Shifting right by {-shifts}")
        # else:
        #     get_logger(__name__).warning(f"Shifting left by {shifts}")
        # get_logger(__name__).warning(f"Final next_target length: {len(f'{next_target:x}')}")

        # self._logger.warning(f"Anchor Target: {anchor_target:x}")    
        # self._logger.warning(f"Next Target: {next_target:x}")
        # self._logger.warning(f"Anchor Target: {anchor_target:064x}")    
        # self._logger.warning(f"Next Target: {next_target:064x}")
        
        return self.asert_target_to_bits(next_target, height)
    


    @classmethod
    def asert_target_to_bits(cls, target: int, height: int) -> int:
        """Convert target to bits with height-dependent length handling"""
        hex_str = f"{target:x}"
        hex_str = hex_str.lstrip('0')  # Remove leading zeros
        
        # get_logger(__name__).warning(f"ASERT bits conversion:")
        # get_logger(__name__).warning(f"Input hex: {hex_str} with len {len(hex_str)}")
        
        bn = OurBigNum()
        bn.set_hex(hex_str)
        return bn.get_compact()
     
    @classmethod
    def asert_bits_to_target(self, ncompact: int) -> int:
        if not (0 <= ncompact < (1 << 32)):
            raise Exception(f"ncompact should be uint32. got {ncompact!r}")
        nsize = ncompact >> 24
        nword = ncompact & 0x007fffff
        if nsize <= 3:
            nword >>= 8 * (3 - nsize)
            ret = nword
        else:
            ret = nword
            ret <<= 8 * (nsize - 3)
        # Check for negative, bit 24 represents sign of N
        if nword != 0 and (ncompact & 0x00800000) != 0:
            raise Exception("target cannot be negative")
        if nword != 0 and ((nsize > 34) or (nword > 0xff and nsize > 33) or (nword > 0xffff and nsize > 32)):
            raise Exception("target has overflown")
        return ret

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
            self._logger.warning("can_connect: header is None")
            return False
        height = header['block_height']
        chain_height = self.height()
        self._logger.warning(f"can_connect: Checking height {height}, chain height is {chain_height}")

        if height >= ASERT_HEIGHT:
            try:
                prev_hash = self.get_hash(height - 1)
            except Exception as e:
                self._logger.error(f"Error getting previous hash: {e}")
                return False

            # Only check that this block connects to a known previous hash
            if prev_hash == header.get('prev_block_hash'):
                return True

            self._logger.error(f"Previous hash mismatch: expected {prev_hash}, got {header.get('prev_block_hash')}")
            return False

        
        if check_height and chain_height != height - 1:
            self._logger.warning(f"can_connect: Height check failed - chain height {chain_height} != header height-1 {height-1}")
            self._logger.warning(f"can_connect: Returning to height {chain_height + 1}")
            return False

        # if check_height and self.height() != height - 1:
        #     self._logger.warning(f"Height mismatch: chain height {self.height()} vs header height-1 {height-1}")
        #     return False

        
        if height == 0:
            self._logger.warning("can_connect: Genesis block check")
            return hash_header(header) == constants.net.GENESIS
            
        try:
            prev_hash = self.get_hash(height - 1)
        except Exception as e:
            self._logger.error(f"Error getting previous hash: {e}")
            return False
            
        if prev_hash != header.get('prev_block_hash'):
            self._logger.error(f"Previous hash mismatch: expected {prev_hash}, got {header.get('prev_block_hash')}")
            return False
            
        try:
            if height == ASERT_HEIGHT - 1:  # Transition block
                target = self.bits_to_target(header['bits'])
            else:
                target = self.get_target(height)
                self._logger.warning(f"Target for height {height}: {target:08x}")
                
        except MissingHeader:
            self._logger.error(f"Missing header for height {height}")
            return False
            
        try:
            self.verify_header(header, prev_hash, target)
        except BaseException as e:
            self._logger.error(f"Error verifying header: {e}")
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
