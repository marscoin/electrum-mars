def bn_to_compact(bn):
    # Assume `bn` is a large integer in Python
    # Simulating BN_bn2mpi:
    bn_bytes = bn.to_bytes((bn.bit_length() + 7) // 8, byteorder='big', signed=False)
    mpi_size = len(bn_bytes) + 4  # Adding 4 bytes for the MPI prefix

    # Simulating the creation of the compact format:
    if mpi_size <= 3:
        compact = int.from_bytes(bn_bytes, byteorder='big') << (8 * (3 - mpi_size))
    else:
        compact = int.from_bytes(bn_bytes[:3], byteorder='big')

    # Add size to the high byte of the compact representation
    compact |= (mpi_size - 4) << 24  # Subtracting 4 bytes to ignore the MPI prefix size
    return compact

# Example usage:
large_number = 0xfcfef03010000000000000000000000000000000000000000000000
compact_format = bn_to_compact(large_number)
print("Compact format:", hex(compact_format))

hex_input = "fcfef03010000000000000000000000000000000000000000000000"
print(hex_input)
print("Result should be: 504352751 (0x1e0fcfef)")
print("Final bits: {} (0x{:x})".format(compact_format, compact_format))


import struct

def bn2mpi_to_compact(bn):
    """Converts a big-endian MPI (as produced by BN_bn2mpi) to Bitcoin's compact format."""

    # Convert the MPI to a bytes object
    bn_bytes = bytes.fromhex(bn[4:].hex())

    # Determine the exponent and mantissa
    size = len(bn_bytes)
    if size <= 3:
        mantissa = int.from_bytes(bn_bytes, 'big')
        exponent = 0
    else:
        mantissa = int.from_bytes(bn_bytes[:3], 'big')
        exponent = size - 3

    # Pack the compact format
    return struct.pack("<I", (exponent << 24) | mantissa)

import binascii

# Your hexadecimal string
hex_str = "fcfef0301000000000000000000000000000000000000000000000"

# Convert hex string to bytes
data_bytes = binascii.unhexlify(hex_str)

# Prepare a header or a specific format, e.g., assuming the first byte indicates length
# and you want to include only the first few bytes of data
# Let's include the first 3 bytes of data for demonstration (similar to your example)
length = 3
header = length.to_bytes(4, byteorder='big')  # 4-byte big-endian length
formatted_data = header + data_bytes[:length]

# Convert to a suitable Python representation
bn = formatted_data

# Print in the escape format to visualize as in your example
print("bn =", bn)


#bn = b'\x00\x00\x00\x03\x02\x00\x00\x00'
compact = bn2mpi_to_compact(bn)
print(compact.hex()) 

def serialize_to_mpi(bn):
    # Simulate BN_bn2mpi: Serialize the big number to bytes with a 4-byte length prefix
    bn_bytes = bn.to_bytes((bn.bit_length() + 7) // 8, 'big')
    mpi_size = len(bn_bytes) + 4  # Adding 4 bytes for the prefix
    return mpi_size, bn_bytes

def get_compact_from_mpi(bn):
    mpi_size, bn_bytes = serialize_to_mpi(bn)
    mpi_size -= 4  # Adjust for the prefix size

    # Initialize the compact format by shifting the size into the highest byte
    nCompact = mpi_size << 24

    # Conditional byte inclusion based on the actual size of the data
    if mpi_size >= 1:
        nCompact |= bn_bytes[0] << 16  # First byte after the prefix
    if mpi_size >= 2:
        nCompact |= bn_bytes[1] << 8   # Second byte
    if mpi_size >= 3:
        nCompact |= bn_bytes[2]        # Third byte

    return nCompact

# Example usage
large_number = int("fcfef03010000000000000000000000000000000000000000000000", 16)
compact_format = get_compact_from_mpi(large_number)
print("Final bits: {} (0x{:x})".format(compact_format, compact_format))
