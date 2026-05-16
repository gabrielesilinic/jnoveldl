*This project is not associated with j-novel club or represents j-novel club in any way. Its name is only indicative of its function*

# About jnoveldl

jnoveldl is a command line tool to download/backup jnovel's legally purchused premium edition novels and to also synthesize from those the TTS such that one may consume any of their novels on the go. 

It outputs a properly formatted .m4b audiobook with chapter markers though it has some minor quirks due to the ebook structure those ebooks tend to have. nonetheless it is pretty good in my opinion.

This project relies on Kokoro TTS and at the moment supports only english. this is a personal project and I may not always have the time and care in accepting PRs or expanding it much, it is strongly for my own use but I publushed anyway as one has expressed intrest in it.

I thank jnovel club for allowing third party developers access to their API, this project is meant for personal use and I purpusefully rate limited it to avoid any undue stress on their servers, I would ask others who fork or contribute to keep it like that.

# Usage
To use jnovel dl, you will need to have Python 3 installed on your system. You can then install the required dependencies using pip:

```bash
pip install -r requirements.txt
```
Once you have the dependencies installed, you can run the tool using the command line. The basic usage is as follows:

```bash
python tui.py
```

at this point it should let you input credentials and such at the first run. note that this project tries to use a keyring whenever possible however it has cases when it may not. it is not exactly the height of security however I have to keep it as such due to different platform APIs and my desire to be able to use it via an SSH session where stuff like the gnome keyring may not be available.

# Support
This project is Linux first. I will never purpusefully break support for windows or mac but I may not be able to test on those platforms or not willing to. this project is meant to run on an Ubuntu 24.04 workstation (ubuntu desktop) mounting an AMD 7900xtx via Pytorch ROCM first and foremost. however it is not like it won't work elsewhere.