
![Logo](https://i.imgur.com/Oawmq1d.jpeg
)




***This is still a major WIP, expect bugs!*** SWA allows users to scrape and download workshop items from the Steam Workshop using steamcmd.exe, saving the data to a CSV file and providing an option to resume the download if the data file already exists.


## Installation

This project runs on Python 3 paired with SteamCMD.

Download the steamcmd.exe tool from the official SteamCMD website (https://developer.valvesoftware.com/wiki/SteamCMD) and note down the full file path, as you'll need this later.


## Deployment

To use this script, clone or download SWA to your local machine and run it.

```
  python location/to/steam-workschop-archiver.py
```
From here, the script will prompt you to enter the full path of your SteamCMD.exe installation

```
Enter the full path for steamcmd.exe: C:\Users\Desktop\steamcmd\steamcmd.exe
```
Next, SWA will ask for the app id for which game whose workshop you want to download. You can find this number on a website like: https://steamdb.info/apps/. As of now, SWA only supports games in which you can download workshop items anonymously.
```
Enter the app id for the game whose workshop you want to download (https://steamdb.info/apps/): 311210
```
SWA will now tell you what game you have selected to scrape and download. If this isn't correct, close the script and double check your app id.
```
Selected: Call of Duty: Black Ops III
```
Now we have to enter out download path. This is where your workshop items will end up. Make sure you have enough storage.
```
Enter your download path (where the games will be installed): C:\Users\Desktop\workshop\downloads
```
Next the script will ask you how many pages to scrape, this directly corilates to this page on steam (https://steamcommunity.com/workshop/browse/?appid=311210&browsesort=trend&section=readytouseitems&days=-1&actualsort=trend&p=1). I'd sugest using a VPN to do this as you wouldn't want to risk an IP ban from steam for scraping.

If you have canceled a download or closed your terminal before the queue was finishied, SWA will ask you if you'd like to resume, you can simple select Y/N. Selecting Y will also check for updates with all of the workshop content.

From there, SWA will ask you if you're ready to start your download, and then you should be off to the races :)
