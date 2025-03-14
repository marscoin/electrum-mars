# -*- mode: python -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

import sys, os

PACKAGE='Electrum-MARS'
PYPKG='electrum_mars'
MAIN_SCRIPT='run_electrum'
ICONS_FILE=PYPKG + '/gui/icons/electrum.icns'

VERSION='28.1.1.0'

electrum = os.path.abspath(".") + "/"
electrum = '/Users/novalis78/Projects/electrum-mars/'
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
hiddenimports += ['_scrypt', 'PyQt5.QtPrintSupport','scrypt', 'cryptography','libusb', 'dns', 'certifi','qrcode','protobuf','aiohttp','aiohttp_socks', 'aiorpcx','kivy','bitstring','secp256k1prp']  # needed by Revealer

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
#binaries = [(electrum + "contrib/osx/libusb-1.0.dylib", ".")]
binaries = [(electrum + "contrib/osx/libsecp256k1.0.dylib", ".")]
binaries += [(electrum + "contrib/osx/libzbar.0.dylib", ".")]

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
              electrum+'electrum_mars/plugins/email_requests/qt.py',
              electrum+'electrum_mars/plugins/trezor/qt.py',
              electrum+'electrum_mars/plugins/safe_t/client.py',
              electrum+'electrum_mars/plugins/safe_t/qt.py',
              electrum+'electrum_mars/plugins/keepkey/qt.py',
              electrum+'electrum_mars/plugins/ledger/qt.py',
              electrum+'electrum_mars/plugins/coldcard/qt.py',
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
        'NSSupportsAutomaticGraphicsSwitching': 'True'
    },
)
