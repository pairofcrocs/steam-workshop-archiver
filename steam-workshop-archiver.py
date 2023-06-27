import requests
import csv
import re
import subprocess
import sys
import os

#################################set variables###################################
page = 1
# ANSI escape code for green color
GREEN_COLOR = "\033[92m"
# ANSI escape code to reset text color
RESET_COLOR = "\033[0m"
#################################set variables###################################

# enter steamcmd.exe path
steamcmd_path = input("Enter the full path for steamcmd.exe: ")

# enter app id
appid = input("Enter the app id for the game which workshop you want to download (https://steamdb.info/apps/): ")

# url to scrape
url = "https://steamcommunity.com/workshop/browse/?appid=" + appid + "&browsesort=trend&section=readytouseitems&created_date_range_filter_start=0&created_date_range_filter_end=0&updated_date_range_filter_start=0&updated_date_range_filter_end=0&actualsort=trend&p={page}&days=-1"

# scrape game name from (url)
response = requests.get(url)
pattern = r'<div class="apphub_AppName ellipsis">\s*(.*?)\s*</div>'
content = re.findall(pattern, response.text)

# Print the extracted game name
for item in content:
    print(f'{GREEN_COLOR}Selected: {item}{RESET_COLOR}')

# download location
force_install_dir = input("Enter your download path (where the games will be installed): ")

# creates file path for csv
workshop_list_file = force_install_dir + '/' + appid + '_data' + '.csv'

# extracts workshop id from urls
def extract_workshop_id(url):
    start_index = url.find('id=') + 3
    end_index = url.find('&', start_index)
    if end_index == -1:
        return url[start_index:]
    else:
        return url[start_index:end_index]

def download_workshop_items(workshop_list_file):
    print(f'{GREEN_COLOR}Download Queue Starting...{RESET_COLOR}')
    with open(workshop_list_file, 'r', encoding='utf-8') as file:
        reader = csv.reader(file)
        workshop_items = [(row[0], row[1]) for index, row in enumerate(reader) if index != 0]

    total_items = len(workshop_items)

    # Run steamcmd and pass the workshop item IDs to download
    steamcmd_command = [
        steamcmd_path,
        '+force_install_dir', force_install_dir,
        '+login', 'anonymous',
    ]

    for i, (file_name, url) in enumerate(workshop_items, start=1):
        workshop_id = extract_workshop_id(url)

        steamcmd_command.append('+workshop_download_item')
        steamcmd_command.append(appid)
        steamcmd_command.append(workshop_id)
        steamcmd_command.append('+quit')  # Add the +quit command to terminate the session

        # Execute steamcmd command and capture the output
        process = subprocess.Popen(steamcmd_command, stdout=subprocess.PIPE, universal_newlines=True)

        # Display steamcmd output and progress in real-time
        progress_text = None
        for line in process.stdout:
            line = line.strip()
            if line.startswith('Downloading item'):
                # Display progress text when download starts
                progress_text = f'{GREEN_COLOR}{i}/{total_items}{RESET_COLOR} - {line} {GREEN_COLOR}{file_name}{RESET_COLOR}'
            elif line.startswith('Success.'):
                # Clear progress text when download completes
                progress_text = None

            if progress_text:
                print(progress_text)

            sys.stdout.flush()

        # Wait for steamcmd to finish
        process.wait()

        # Clear the steamcmd command list for the next iteration
        steamcmd_command = [
            steamcmd_path,
            '+force_install_dir', force_install_dir,
            '+login', 'anonymous',
        ]

def webscraping():
    page=1
    # Get the number of pages to scrape from user input
    num_pages = input("Enter the number of pages to scrape (or 'all' to scrape all available pages): ")
    if num_pages.lower() == "all":
        num_pages = -1
    else:
        num_pages = int(num_pages)
    
    
    # Variables to store the extracted data
    titles = []
    authors = []
    links = []

    # Scrape the pages until reaching the specified number of pages or no items found
    while num_pages != 0:
        # Update the URL with the current page
        page_url = url.format(page=page)

        # Send a GET request to the current page URL
        response = requests.get(page_url)

        # Check if the request was successful
        if response.status_code == 200:
            # Extract specific data from the response content
            html_content = response.text

            # Check if no items matching the search criteria are found
            if "No items matching your search criteria were found." in html_content:
                print("No more items found. Exiting...")
                break

            # Extract titles, authors, and links using regular expressions
            title_pattern = r'<div class="workshopItemTitle ellipsis">(.*?)<\/div>'
            author_pattern = r'<div class="workshopItemAuthorName ellipsis">by&nbsp;<a class="workshop_author_link" href=".*?">(.*?)<\/a><\/div>'
            link_pattern = r'<a data-panel="{&quot;focusable&quot;:false}" href="(.*?)" class="item_link">'

            titles.extend(re.findall(title_pattern, html_content, re.DOTALL))
            authors.extend(re.findall(author_pattern, html_content, re.DOTALL))
            links.extend(re.findall(link_pattern, html_content, re.DOTALL))

            print(f"Scraped page {page}")

            # Check if scraping all available pages
            if num_pages != -1:
                num_pages -= 1
                if num_pages == 0:
                    break

            # Increment the page count
            page += 1

        else:
            print("Failed to retrieve the webpage.")
            break

    # Save titles, authors, and links to a CSV file
    file_name = appid + "_data.csv"
    file_path = f"{force_install_dir}\\{file_name}"
    data = zip(titles, links, authors)
    with open(file_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Title", "Link", "Author"])  # Write header row
        writer.writerows(data)

    print(f"Titles, authors, and links saved to: {file_path}")

# checks to see if csv file exists.
if os.path.exists(workshop_list_file):
    resume_download = input("The workshop list file already exists. Do you want to resume downloading? (y/n): ")
    if resume_download.lower() == 'y':
        download_workshop_items(workshop_list_file)
    elif resume_download.lower() == 'n':
        print("Resuming download skipped.")
        webscraping()
    else:
        print("invalid input")

else:
    webscraping()

# Call the function to download the workshop items
start_download = input("Ready to start the download? (y/n): ")

if start_download.lower() == "y":
    download_workshop_items(workshop_list_file)
else:
    print("Download canceled.")
