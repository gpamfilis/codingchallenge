import csv
import json
import logging
import os
import time
import warnings
from datetime import datetime
from itertools import chain
from urllib import request

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file
from flask_sqlalchemy import SQLAlchemy
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.firefox.options import Options

warnings.filterwarnings("ignore", category=DeprecationWarning)


logger = logging.getLogger(__name__)
app = Flask(__name__)
db = SQLAlchemy()

options = Options()
options.add_argument("--headless")


def utc_now():
    return datetime.utcnow().__str__().split(".")[0]


class ComponentResults(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64))
    data = db.Column(db.Text)
    datetime_downloaded = db.Column(db.String(100), default=utc_now())


def get_part_from_page(driver, base_url):
    soup = BeautifulSoup(driver.page_source, features="lxml")
    parts_table = soup.find("table", {"class": "divSFListResultsMainTable"})

    parts = parts_table.find_all("tr")
    parts_ls = []
    for part in parts:
        l = part.find("tbody")
        if l is None:
            continue
        else:
            parts_ls.append(l)
    parts_ls = parts_ls[::2]
    part_infos = []
    for part in parts_ls:
        name = part.find("td", {"class": "divSFListResultsItemPartNumber"}).text
        labels = part.find_all("td", {"class": "divSFListResultsItemHeader"})
        values = part.find_all("td", {"class": "divSFListResultsItemValue"})
        clean_labels = []
        order_links = []
        for label in labels:
            clean_label = label.text.rstrip().strip(":").upper()
            clean_labels.append(clean_label)
        clean_labels.append("INVID")
        clean_labels.append("ORDERLINK")
        clean_labels.append("NAMEFOUND")
        jss = part.find("td")
        invid = jss.attrs["onclick"].split(",")[0].split("(")[1]
        order_link = base_url + f"&InvID={invid}"
        order_links.append(order_link)
        clean_values = [v.text.rstrip().strip(":").upper() for v in values]
        clean_values.append(invid)
        clean_values.append(order_link)
        clean_values.append(name)
        part_infos.append(clean_values)

    return clean_labels, part_infos


def get_part_data_from_url(
    driver, partID, distributor, base_url, source_url, logger, strict=True
):
    """
    If we set strict=True it will match the beggining of the part name exactly.
    because when inserting LTC2351CUH-14#PBF into the url the #PBF get removed from the search field.
    """
    driver.get(base_url)
    time.sleep(0.5)
    not_found = "Not Found" in driver.page_source
    if not_found:
        return None
    base_url = driver.current_url

    try:
        pagination = driver.find_element_by_id("WDL2_dlT")
        pages_str = pagination.text.split(" ")
        pages = [int(p) for p in pages_str]
    except NoSuchElementException as exc:
        pages = None
    if pages is not None:
        pgs = []
        for p, ps in enumerate(pages_str):
            if len(ps) == 1:
                pgs.append(f"0{pages[p]}")
            else:
                pgs.append(f"{pages[p]}")

    all_parts = []
    clean_labels, page_part = get_part_from_page(driver, base_url)
    _ = [page.extend([0]) for page in page_part]

    all_parts.append(page_part)
    time.sleep(4)
    if pages is not None:
        for p, pg in enumerate(pgs[:-1]):
            logger(
                f"extracting data from page: {p+1}",
            )
            try:
                driver.find_element_by_id(f"WDL2_dlT_ctl{pg}_lnkT").click()
            except NoSuchElementException:
                driver.refresh()
                time.sleep(4)
                driver.find_element_by_id(f"WDL2_dlT_ctl{pg}_lnkT").click()

            time.sleep(2)
            clean_labels, page_part = get_part_from_page(driver, base_url)
            [page.extend([p + 1]) for page in page_part]
            all_parts.append(page_part)

    time_now = utc_now()
    for page_parts in all_parts:
        [part.append(time_now) for part in page_parts]
        [part.append(source_url) for part in page_parts]

    return all_parts


def setup_driver():
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    driver = webdriver.Chrome(chrome_options=chrome_options)
    return driver


def filter_out_component_names(dfs: list, partID: str) -> list:
    ress = []
    for df in dfs:
        res = list(chain(*df))
        ress.append(res)
    final = list(chain(*ress))
    final_filtered = [fi for fi in final if fi[-4].startswith(partID)]
    return final_filtered


def save_data_to_db(data, partID):
    results = ComponentResults()
    results.name = partID
    results.data = json.dumps(data)
    db.session.add(results)
    try:
        db.session.commit()
    except:

        db.session.rollback()


def save_data_to_file(data, partID, base_dir="../data", custom_file_name=""):
    columns = [
        "MANUFACTURER",
        "DC",
        "STATUS",
        "QUANTITY",
        "INVID",
        "ORDERLINK",
        "NAMEFOUND",
        "PAGENUMBER",
        "DATETIMEDOWNLOADED",
        "SOURCEURL",
    ]
    file_name = f"{base_dir}/{partID}_all_results.csv"
    if custom_file_name:
        file_name = custom_file_name
    with open(file_name, "w", newline="") as f:
        write = csv.writer(f)
        write.writerow(columns)
        write.writerows(data)
    return columns, data


def main(partID, distributors, logger):
    dfs = []
    try:

        for k, v in distributors.items():
            driver = setup_driver()
            logger(k)
            time.sleep(4)
            distributor = k
            base_url = v["base_url"]
            source_url = v["source_url"]
            df = get_part_data_from_url(
                driver,
                partID,
                distributor,
                base_url,
                source_url,
                strict=True,
                logger=logger,
            )
            if df is None:
                logger(
                    f"No component information found for component: {partID} at distributor: {distributor}"
                )
                driver.close()
                continue
            dfs.append(df)
            time.sleep(2)
            driver.close()
    except Exception as e:
        logger(e)
        # driver.close()

    if not dfs:
        print("No data found")
        return None
    return dfs


@app.route("/", methods=["GET"])
def instructions():
    return "Welcome to the component scraper. Please read README.md"


@app.route("/findpart", methods=["POST"])
def findpart():
    data = request.get_json()
    if "partID" not in data:
        return "No component ID provided"
    partID = data["partID"]
    if not partID:
        return "Please provide us with a valid component id"

    app.logger.info(partID)

    distributors = {
        "derf": {
            "base_url": f"https://icsource.com/icsourcewdl/icswdl2.aspx?user=alderf&part={partID}",
            "source_url": "https://www.derf.com/",
        },
        "electronicchip": {
            "base_url": f"https://icsource.com/icsourcewdl/icswdl2.aspx?user=ecgmbh&part={partID}",
            "source_url": "https://www.electronic-chip.de/",
        },
        "dynamicsource": {
            "base_url": f"https://icsource.com/icsourcewdl/icswdl2.aspx?user=source1&part={partID}",
            "source_url": "https://www.dynamicsource.com/",
        },
    }
    app.logger.info("distributors")
    dfs = main(partID=partID, distributors=distributors, logger=app.logger.info)
    app.logger.info(f"dfs {dfs}")

    if dfs is None:
        return "No data found"
    data = filter_out_component_names(dfs=dfs, partID=partID)

    save_data_to_file(data, partID=partID)
    save_data_to_db(data, partID=partID)
    return jsonify(data)


@app.route("/retrivefromdb", methods=["POST"])
def retrivefromdb():
    data = request.get_json()
    if "partID" not in data:
        return "No component ID provided"
    partID = data["partID"]
    if not partID:
        return "Please provide us with a valid component id"

    results = ComponentResults.query.filter_by(name=partID).all()
    if results:
        items = []
        for result in results:
            items.append(
                {
                    "data": json.loads(result.data),
                    "datetime_downloaded": result.datetime_downloaded,
                }
            )
        return jsonify(items)

    return "No Data Found"


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    if "partID" not in data:
        return "No component ID provided"
    partID = data["partID"]
    if not partID:
        return "Please provide us with a valid component id"
    path = f"../data/{partID}_all_results.csv"
    app.logger.info(path)
    file = os.path.isfile(path)
    if not file:
        return "File does not exist. Please download file."
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.config.update({"SQLALCHEMY_DATABASE_URI": "sqlite:///../data/alpas.sqlite"})
    db.init_app(app)
    db.app = app
    db.create_all()
    app.run(host="0.0.0.0", debug=True)
