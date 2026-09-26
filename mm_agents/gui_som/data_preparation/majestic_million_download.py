import csv

import requests


def download_csv(url, file_path):
    response = requests.get(url)
    with open(file_path, 'w', newline='', encoding='utf-8') as file:
        file.write(response.text)


def read_csv(file_path):
    urls = []
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.reader(csvfile)
        next(reader, None)
        for row in reader:
            urls.append(row[2])
    return urls


def main():
    url = 'http://downloads.majestic.com/majestic_million.csv'
    file_path = 'majestic_million.csv'

    print("Downloading Majestic Million CSV...")
    download_csv(url, file_path)

    print("Reading URLs from CSV...")
    urls = read_csv(file_path)

    for url in urls[:10]:
        print(url)


if __name__ == "__main__":
    main()
