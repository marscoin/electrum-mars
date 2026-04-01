# -*- mode: python -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

import sys, os

PACKAGE='Electrum-Mars'
PYPKG='electrum_mars'
MAIN_SCRIPT='run_electrum'
ICONS_FILE=PYPKG + '/gui/icons/electrum.icns'


VERSION = os.environ.get("ELECTRUM_VERSION")
if not VERSION:
    raise Exception('no version')

electrum = os.path.abspath(".") + "/"
block_cipher = None

# see https://github.com/pyinstaller/pyinstaller/issues/2005
hiddenimports = []
hiddenimports += collect_submodules('pkg_resources')  # workaround for https://github.com/pypa/setuptools/issues/1963
hiddenimports += collect_submodules('trezorlib')
hiddenimports += collect_submodules('safetlib')
hiddenimports += collect_submodules('btchip')
hiddenimports += collect_submodules('keepkeylib')
hiddenimports += collect_submodules('websocket')
hiddenimports += collect_submodules('ckcc')
hiddenimports += collect_submodules('bitbox02')
hiddenimports += ['electrum_mars.plugins.jade.jade']
hiddenimports += ['electrum_mars.plugins.jade.jadepy.jade']
hiddenimports += ['_scrypt', 'PyQt5.QtPrintSupport']  # needed by Revealer
hiddenimports += collect_submodules('bitstring')
hiddenimports += ['electrum_mars.plugins.atomic_swap']
hiddenimports += ['electrum_mars.plugins.atomic_swap.qt']
hiddenimports += ['electrum_mars.plugins.atomic_swap.swap_engine']
hiddenimports += ['electrum_mars.plugins.atomic_swap.orderbook']
hiddenimports += ['electrum_mars.atomic_swap_htlc']
hiddenimports += ['electrum_mars.btc_monitor']

datas = [
    (electrum + PYPKG + '/*.json', PYPKG),
    (electrum + PYPKG + '/lnwire/*.csv', PYPKG + '/lnwire'),
    (electrum + PYPKG + '/wordlist/english.txt', PYPKG + '/wordlist'),
    (electrum + PYPKG + '/wordlist/slip39.txt', PYPKG + '/wordlist'),
    (electrum + PYPKG + '/locale', PYPKG + '/locale'),
    (electrum + PYPKG + '/plugins', PYPKG + '/plugins'),
    (electrum + PYPKG + '/gui/icons', PYPKG + '/gui/icons'),
]
datas += collect_data_files('trezorlib')
datas += collect_data_files('safetlib')
datas += collect_data_files('btchip')
datas += collect_data_files('keepkeylib')
datas += collect_data_files('ckcc')
datas += collect_data_files('bitbox02')

# Add libusb so Trezor and Safe-T mini will work
binaries = [(electrum + "electrum_mars/libusb-1.0.dylib", ".")]
binaries += [(electrum + "electrum_mars/libsecp256k1.0.dylib", ".")]
binaries += [(electrum + "electrum_mars/libzbar.0.dylib", ".")]

# Workaround for "Retro Look":
binaries += [b for b in collect_dynamic_libs('PyQt5') if 'macstyle' in b[0]]

# We don't put these files in to actually include them in the script but to make the Analysis method scan them for imports
a = Analysis([electrum+ MAIN_SCRIPT,
              electrum+'electrum_mars/gui/qt/main_window.py',
              electrum+'electrum_mars/gui/qt/qrreader/qtmultimedia/camera_dialog.py',
              electrum+'electrum_mars/gui/text.py',
              electrum+'electrum_mars/util.py',
              electrum+'electrum_mars/wallet.py',
              electrum+'electrum_mars/simple_config.py',
              electrum+'electrum_mars/bitcoin.py',
              electrum+'electrum_mars/blockchain.py',
              electrum+'electrum_mars/dnssec.py',
              electrum+'electrum_mars/commands.py',
              electrum+'electrum_mars/plugins/cosigner_pool/qt.py',
              electrum+'electrum_mars/plugins/trezor/qt.py',
              electrum+'electrum_mars/plugins/safe_t/client.py',
              electrum+'electrum_mars/plugins/safe_t/qt.py',
              electrum+'electrum_mars/plugins/keepkey/qt.py',
              electrum+'electrum_mars/plugins/ledger/qt.py',
              electrum+'electrum_mars/plugins/coldcard/qt.py',
              electrum+'electrum_mars/plugins/jade/qt.py',
              ],
             binaries=binaries,
             datas=datas,
             hiddenimports=hiddenimports,
             hookspath=[])

# http://stackoverflow.com/questions/19055089/pyinstaller-onefile-warning-pyconfig-h-when-importing-scipy-or-scipy-signal
for d in a.datas:
    if 'pyconfig' in d[0]:
        a.datas.remove(d)
        break

# Fix conflicting scrypt-bundled OpenSSL that clashes with Python's _ssl module.
# Remove ALL OpenSSL libs that PyInstaller found (scrypt bundles a conflicting copy),
# then add brew's OpenSSL explicitly. Scrypt will use brew's version at runtime.
import subprocess
brew_prefix = subprocess.check_output(['brew', '--prefix', 'openssl']).decode().strip()
for x in a.binaries.copy():
    if 'libcrypto' in x[0] or 'libssl' in x[0]:
        a.binaries.remove(x)
        print('----> Removed:', x[0])
# Add brew's OpenSSL
a.binaries.append(('libcrypto.3.dylib', brew_prefix + '/lib/libcrypto.3.dylib', 'BINARY'))
a.binaries.append(('libssl.3.dylib', brew_prefix + '/lib/libssl.3.dylib', 'BINARY'))
print('----> Added brew OpenSSL from', brew_prefix)

# Strip out parts of Qt that we never use. Reduces binary size by tens of MBs. see #4815
qt_bins2remove=('qtweb', 'qt3d', 'qtgame', 'qtdesigner', 'qtquick', 'qtlocation', 'qttest', 'qtxml')
print("Removing Qt binaries:", *qt_bins2remove)
for x in a.binaries.copy():
    for r in qt_bins2remove:
        if x[0].lower().startswith(r):
            a.binaries.remove(x)
            print('----> Removed x =', x)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name=MAIN_SCRIPT,
    debug=False,
    strip=False,
    upx=True,
    icon=electrum+ICONS_FILE,
    console=False,
    target_arch='arm64',
)

app = BUNDLE(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    version = VERSION,
    name=PACKAGE + '.app',
    icon=electrum+ICONS_FILE,
    bundle_identifier=None,
    info_plist={
        'NSHighResolutionCapable': 'True',
        'NSSupportsAutomaticGraphicsSwitching': 'True',
        'CFBundleURLTypes':
            [{
                'CFBundleURLName': 'marscoin',
                'CFBundleURLSchemes': ['marscoin', ],
            }],
        'LSMinimumSystemVersion': '10.13.0',
        'NSCameraUsageDescription': 'Electrum would like to access the camera to scan for QR codes',
    },
)
