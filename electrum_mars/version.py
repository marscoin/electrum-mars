ELECTRUM_VERSION = '1.6'     # version of the client package
APK_VERSION = '1.6.0.0'        # read by buildozer.spec

PROTOCOL_VERSION = '1.4'     # protocol version requested

# The hash of the mnemonic seed must begin with this
SEED_PREFIX        = '01'      # Standard wallet


def seed_prefix(seed_type):
    if seed_type == 'standard':
        return SEED_PREFIX
    raise Exception(f"unknown seed_type: {seed_type}")
