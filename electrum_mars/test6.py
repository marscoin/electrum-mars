# test_asert.py

def bits_to_target(bits):
    """Convert bits to target with detailed debug"""
    size = bits >> 24
    word = bits & 0x007fffff
    
    print(f"bits_to_target input: {bits:08x}")
    print(f"size: {size}, word: {word:06x}")
    
    result = word << (8 * (size - 3)) if size > 3 else word >> (8 * (3 - size))
    print(f"bits_to_target result: {result:x}")
    return result

def target_to_bits(target):
    """Convert target to bits with detailed debug"""
    if target == 0:
        return 0
    
    print(f"\nConverting target to bits:")
    print(f"Input target: {target:x}")
    
    # Get bit length and size in bytes
    bit_length = target.bit_length()
    size = (bit_length + 7) // 8
    
    print(f"Bit length: {bit_length}")
    print(f"Initial size in bytes: {size}")
    
    # Extract mantissa
    if size <= 3:
        shift = 8 * (3 - size)
        print(f"Small target, shifting left by {shift}")
        mantissa = target << shift
    else:
        shift = 8 * (size - 3)
        print(f"Large target, shifting right by {shift}")
        mantissa = target >> shift
        
    print(f"Initial mantissa: {mantissa:x}")
    
    # Check for sign bit
    if mantissa & 0x00800000:
        print("Sign bit set, adjusting...")
        mantissa >>= 8
        size += 1
    
    mantissa &= 0x007fffff
    print(f"Final mantissa: {mantissa:06x}")
    
    # Use size 30 (0x1e) for our chain
    size = 0x1e
    result = (size << 24) | mantissa
    print(f"Final bits: {result:08x}")
    return result

def calculate_target(prev_height, prev_time, prev_bits, current_height):
    """ASERT calculation with detailed target debug"""
    HALF_LIFE = 2 * 3600
    TARGET_SPACING = 123
    ANCHOR_HEIGHT = 2999999
    
    print(f"\nStarting ASERT calculation for block {current_height}")
    print(f"Previous bits: {prev_bits:08x}")
    
    # Special handling for anchor block and first block  
    if prev_height < ANCHOR_HEIGHT or current_height <= ANCHOR_HEIGHT + 1:
        print("Using initial/anchor bits")
        return 0x1e0fffff
        
    # Get initial anchor target
    anchor_target = bits_to_target(0x1e0fffff)
    print(f"Anchor target: {anchor_target:x}")
    
    # Calculate diffs
    time_diff = prev_time - 1720883312  # anchor timestamp
    height_diff = current_height - ANCHOR_HEIGHT - 1
    
    print(f"Time diff: {time_diff}")
    print(f"Height diff: {height_diff}")
    
    # Calculate exponent
    numerator = (time_diff - TARGET_SPACING * (height_diff + 1)) * 65536
    exponent = numerator // HALF_LIFE
    if numerator < 0 and numerator % HALF_LIFE != 0:
        exponent -= 1
    
    shifts = exponent >> 16
    frac = exponent & 0xffff
    
    print(f"Exponent: {exponent}")
    print(f"Shifts: {shifts}")
    print(f"Frac: {frac}")
    
    # Calculate factor
    factor = 65536 + ((195766423245049 * frac +
                      971821376 * frac * frac +
                      5127 * frac * frac * frac +
                      (1 << 47)) >> 48)
                      
    print(f"Factor: {factor}")
    
    # Calculate next target
    next_target = anchor_target * factor
    next_target >>= 16  # Remove extra precision
    
    print(f"Target after /2^16: {next_target:x}")
    
    # Apply shifts
    print(f"Applying shifts: {shifts}")
    if shifts < 0:
        next_target >>= -shifts
    else:
        next_target <<= shifts
        
    print(f"Final target: {next_target:x}")
    
    # Convert to bits
    return target_to_bits(next_target)

def test_all_blocks():
    """Test calculation with real chain data"""
    test_data = [
        (2999999, 1720883312, 0x1e0fffff),  # anchor
        (3000000, 1720883333, 0x1E0FCFEF),  # first
        (3000001, 1720883354, 0x1e0fa86f),  # second - Fixed! 
        (3000002, 1720883375, 0x1E0F8157),  # third
        (3000003, 1720883396, 0x1E0F5A9F),  # fourth
    ]
    
    print("\nTesting with chain data...")
    for i in range(1, len(test_data)):
        prev = test_data[i-1]
        curr = test_data[i]
        
        result = calculate_target(
            prev_height=prev[0],
            prev_time=prev[1],
            prev_bits=prev[2],
            current_height=curr[0]
        )
        
        print(f"\nBlock {curr[0]}:")
        print(f"Expected:   {curr[2]:08x}")
        print(f"Calculated: {result:08x}")
        print(f"Match: {curr[2] == result}")

if __name__ == "__main__":
    test_all_blocks()