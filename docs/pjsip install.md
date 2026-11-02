## Needed on macOS

This project uses the PJSIP/PJSUA2 open-source license path and is distributed as
`GPL-2.0-or-later`. Keep PJSIP license notices and corresponding source availability in mind when
redistributing builds that include or require these bindings.

1. build tools

        xcode-select --install
    
        brew install swig pkg-config openssl@3

2. Optional but useful for video smoke


        brew install sdl2 ffmpeg openh264

3. Download and install pjproject

        git clone https://github.com/pjsip/pjproject.git
        cd pjproject
        printf '#define PJSUA_MAX_ACC 32\n' > pjlib/include/pj/config_site.h
        ./configure CFLAGS="-fPIC" --with-ssl="$(brew --prefix openssl@3)"
        make dep
        make
   To resolve (version) issues with `ffmpeg` configure with `--disable-ffmpeg`

   PJSUA defaults to eight accounts. Set `PJSUA_MAX_ACC` in `config_site.h` before building when a run needs
   more accounts; the value must be at least the number of clients registered in one endpoint. Rebuild both
   pjproject and the PJSUA2 Python bindings after changing it, since the limit is compiled into the native
   libraries.

4. Build/install the Python SWIG module into the same Python env used by this project:

        cd <project directory where we need the PJSIPUA2 package> 
        uv pip install --python .venv/bin/python setuptools wheel
        
        cd pjproject/pjsip-apps/src/swig/python
        make distclean
        make PYTHON_EXE=<project directory where we need the PJSIPUA2 package>/.venv/bin/python
        <project directory where we need the PJSIPUA2 package>.venv/bin/python setup.py install

   For example:


          cd ~/Documents/workspace/pjproject/pjsip-apps/src/swig/python
          make PYTHON_EXE=~/Documents/workspace/sip_client/.venv/bin/python
          ~/Documents/workspace/sip_client/.venv/bin/python setup.py install

   After changing `PJSUA_MAX_ACC` or rebuilding pjproject, run `make distclean` before building the bindings so
   the extension is relinked against the new static libraries. Add `--force` to the `setup.py install` command to
   overwrite an existing installation.

Alternative: install with uv

      cd ~/Documents/workspace/pjproject/pjsip-apps/src/swig/python   
      make distclean
      make wheel PYTHON_EXE=/Users/jkrohn/Library/CloudStorage/Dropbox/ln/workspace/sip_client/.venv/bin/python

      cd /Users/jkrohn/Library/CloudStorage/Dropbox/ln/workspace/sip_client
      uv pip install --reinstall /Users/jkrohn/Documents/workspace/pjproject/pjsip-apps/src/swig/python/dist/pjsua2-2.17.dev0-cp313-cp313-macosx_11_0_arm64.whl

5. Verify the installation

   In the project directory:

        python -c "import pjsua2; print('ok')"
