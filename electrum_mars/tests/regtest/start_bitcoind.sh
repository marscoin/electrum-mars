#!/usr/bin/env bash
export HOME=~
set -eux pipefail
mkdir -p ~/.marscoin
cat > ~/.marscoin/marscoin.conf <<EOF
regtest=1
txindex=1
printtoconsole=1
rpcuser=doggman
rpcpassword=donkey
rpcallowip=127.0.0.1
zmqpubrawblock=tcp://127.0.0.1:28332
zmqpubrawtx=tcp://127.0.0.1:28333
fallbackfee=0.0002
[regtest]
rpcbind=0.0.0.0
rpcport=18554
EOF
rm -rf ~/.marscoin/regtest
screen -S marscoind -X quit || true
screen -S marscoind -m -d marscoind -regtest
sleep 6
marscoin-cli createwallet test_wallet
addr=$(marscoin-cli getnewaddress)
marscoin-cli generatetoaddress 150 $addr > /dev/null
