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
        ./configure CFLAGS="-fPIC" --with-ssl="$(brew --prefix openssl@3)"
        make dep
        make

4. Build/install the Python SWIG module into the same Python env used by this project:

        cd <project directory where we need the PJSIPUA2 package> 
        uv pip install --python .venv/bin/python setuptools wheel
        
        cd pjproject/pjsip-apps/src/swig/python
        make PYTHON_EXE=<project directory where we need the PJSIPUA2 package>/.venv/bin/python
        <project directory where we need the PJSIPUA2 package>.venv/bin/python setup.py install

   For example:


          cd ~/Documents/workspace/pjproject/pjsip-apps/src/swig/python
          make PYTHON_EXE=~/Documents/workspace/sip_client/.venv/bin/python
          ~/Documents/workspace/sip_client/.venv/bin/python setup.py install

Alternative: install with uv

      cd pjproject/pjsip-apps/src/swig/python   
      make PYTHON_EXE=/Users/jkrohn/Library/CloudStorage/Dropbox/ln/workspace/sip_client/.venv/bin/python

      cd /Users/jkrohn/Library/CloudStorage/Dropbox/ln/workspace/sip_client
      uv pip install /Users/jkrohn/Documents/workspace/pjproject/pjsip-apps/src/swig/python/dist/pjsua2-2.17.dev0-cp313-cp313-macosx_11_0_arm64.whl 

5. Verify the installation

   In the project directory:

        python -c "import pjsua2; print('ok')"
