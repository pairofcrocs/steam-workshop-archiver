import requests
import csv
import re
import subprocess
import sys
import os

# ANSI escape code for green color
GREEN_COLOR = "\033[92m"
# ANSI escape code to reset text color
RESET_COLOR = "\033[0m"

# Enter steamcmd.exe path
steamcmd_path = input("Enter the full path for steamcmd.exe: ")

# Enter app id
appid = input("Enter the app id for the game whose workshop you want to download (https://steamdb.info/apps/): ")

steam_id = "https://steamcommunity.com/workshop/browse/?appid=311210"
steam_response = requests.get(steam_id)
steam_match = re.search(r'<div class="apphub_AppName ellipsis">(.*?)</div>', steam_response.text)
steam_game_title = steam_match.group(1) if steam_match else None

print(f"{GREEN_COLOR}Selected: {steam_game_title}")

# Download location
force_install_dir = input(f"{RESET_COLOR}Enter your download path (where the games will be installed): ")

# Check if the workshop data CSV file exists
workshop_list_file = os.path.join(force_install_dir, f"{appid}_data.csv")
if os.path.exists(workshop_list_file):
    resume_download = input(f"The workshop data file {workshop_list_file} already exists. Do you want to resume the download? (y/n): ")
    if resume_download.lower() == "y":
        print("Resuming download...")
        start_download = "y"
    else:
        print("Starting a fresh download...")
        start_download = "n"
else:
    start_download = "n"

if start_download.lower() != "y":
    # Send a GET request to the Steam Workshop URL

    page = 1
    url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&browsesort=trend&section=readytouseitems&created_date_range_filter_start=0&created_date_range_filter_end=0&updated_date_range_filter_start=0&updated_date_range_filter_end=0&actualsort=trend&p={page}&days=-1"

    # Create csv file in the download location
    workshop_list_file = os.path.join(force_install_dir, f"{appid}_data.csv")

    # Get the number of pages to scrape from user input
    num_pages = input("Enter the number of pages to scrape (or 'all' to scrape all available pages): ")
    num_pages = -1 if num_pages.lower() == "all" else int(num_pages)

    # Variables to store the extracted data
    titles = []
    authors = []
    links = []

    # Scrape the pages until reaching the specified number of pages or no items found
    while num_pages != 0:
        # Update the URL with the current page
        page_url = url.format(page=page)

        # Send a GET request to the current page URL
        with requests.Session() as session:
            response = session.get(page_url)

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
    file_path = os.path.join(force_install_dir, f"{appid}_data.csv")
    data = zip(titles, links, authors)
    with open(file_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Title", "Link", "Author"])  # Write header row
        writer.writerows(data)

    print(f"Titles, authors, and links saved to: {file_path}")

# Prompt the user to start the download
if start_download.lower() != "y":
    start_download = input("Ready to start the download? (y/n): ")
if start_download.lower() != "y":
    sys.exit(0)

def extract_workshop_id(url):
    start_index = url.find('id=') + 3
    end_index = url.find('&', start_index)
    if end_index == -1:
        return url[start_index:]
    else:
        return url[start_index:end_index]

def download_workshop_items(workshop_list_file):
    with open(workshop_list_file, 'r') as file:
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
        steamcmd_command.append(appid)  # Replace 'appid' with the appropriate app ID
        steamcmd_command.append(workshop_id)
        steamcmd_command.append('+quit')  # Add the +quit command to terminate the session

        # Execute steamcmd command and capture the output
        process = subprocess.Popen(steamcmd_command, stdout=subprocess.PIPE, universal_newlines=True)

        # Display steamcmd output and progress in real-time
        progress_text = None
        for line in process.stdout:
            line = line.strip()
            if line == "Waiting for client config...OK":
                # Display progress text when download starts
                progress_text = f'{GREEN_COLOR}{i}/{total_items}{RESET_COLOR} - Downloading item: {file_name}'
            elif line == "Starting download...":
                # Clear progress text when download completes
                progress_text = None

            if line != "-- type 'quit' to exit --":
                if progress_text:
                    print(progress_text)
                else:
                    print(line)

            sys.stdout.flush()

        # Wait for steamcmd to finish
        process.wait()

        # Clear the steamcmd command list for the next iteration
        steamcmd_command = [
            steamcmd_path,
            '+force_install_dir', force_install_dir,
            '+login', 'anonymous',
        ]

# Call the function to download the workshop items
download_workshop_items(workshop_list_file)
