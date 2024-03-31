import multiprocessing
import os
import sys
import json
import time

import parsel
import logging
import datetime
import requests
import http.cookiejar
from multiprocess.exorcise_daemons import ExorcistPool
from exceptions.InvalidCookieException import InvalidCookieException
from data.cache import CsvCacheData, Cache
from iops import file_ops

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def _clean_name(dirty_str):
    allowed_chars = (' ', '_', '.', '-', '[', ']')
    clean = []
    for c in dirty_str.replace('+', '_').replace(':', ' -'):
        if c.isalpha() or c.isdigit() or c in allowed_chars:
            clean.append(c)

    return "".join(clean).strip().rstrip('.')


DEFAULT_TIMEOUT = 5  # seconds


class TimeoutHTTPAdapter(HTTPAdapter):
    timeout = DEFAULT_TIMEOUT

    def __init__(self, *args, **kwargs):
        self.timeout = DEFAULT_TIMEOUT
        if "timeout" in kwargs:
            self.timeout = kwargs["timeout"]
            del kwargs["timeout"]
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


class DownloadLibrary:

    def __init__(self, library_path, cookie_path=None, cookie_auth=None,
                 progress_bar=False, ext_include=None, ext_exclude=None,
                 platform_include=None, purchase_keys=None, trove=False,
                 update=False, content_types=None):

        self.cache_data = {}  # to remove.
        file_ops.set_library_path(library_path)

        self.progress_bar = progress_bar

        self.ext_include = [] if ext_include is None else list(map(lambda s: str(s).lower(), ext_include))  # noqa: E501
        self.ext_exclude = [] if ext_exclude is None else list(map(lambda s: str(s).lower(), ext_exclude))  # noqa: E501

        self.cache_data_csv: Cache = file_ops.load_cache_csv()

        if platform_include is None or 'all' in platform_include:
            # if 'all', then do not need to use this check
            platform_include = []  # why not make the d
        self.platform_include = list(map(lambda s: str(s).lower(), platform_include))

        self.purchase_keys = purchase_keys
        self.trove = trove
        self.update = update
        self.content_types = ['web'] if content_types is None else list(map(str.lower, content_types))  # noqa: E501

        retries = Retry(total=3, backoff_factor=1,
                        status_forcelist=[429, 500, 502, 503, 504])
        timeout_adapter = TimeoutHTTPAdapter(max_retries=retries)

        self.session = requests.Session()
        self.session.mount('http://', timeout_adapter)
        self.session.mount('https://', timeout_adapter)
        if cookie_path:
            try:
                cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
                cookie_jar.load()
                self.session.cookies = cookie_jar
            except http.cookiejar.LoadError:
                # Still support the original cookie method
                with open(cookie_path, 'r') as f:
                    self.session.headers.update({'cookie': f.read().strip()})
        elif cookie_auth:
            self.session.headers.update(
                {'cookie': f'_simpleauth_sess={cookie_auth}'}
            )

    def start(self):
        # todo: convert old cache.
        self.purchase_keys = self.purchase_keys if self.purchase_keys else self._get_purchase_keys()  # noqa: E501

        if self.trove is True:
            logger.info("Only checking the Humble Trove...")
            for product in self._get_trove_products():
                title = _clean_name(product['human-name'])
                self._process_trove_product(title, product)
        else:
            manager = multiprocessing.Manager()
            queue = manager.JoinableQueue()
            with ExorcistPool(multiprocessing.cpu_count()) as pool:

                pool.apply_async(file_ops.update_csv_cache, (queue,))
                jobs = list()
                job_dict = dict()
                for purchase_key in self.purchase_keys:
                    job = pool.apply_async(self._process_order_id,
                                           (purchase_key, queue)
                                           )
                    jobs.append(job)
                    job_dict[purchase_key] = job

                while job_dict:
                    for key in list(job_dict):
                        if job_dict[key].ready():
                            del job_dict[key]
                            # job finished
                    time.sleep(1)

                for job in jobs:
                    job.get()

                queue.put(CsvCacheData("kill", "kill"))

                queue.join()

                pool.close()
                pool.join()

    def _get_trove_download_url(self, machine_name, web_name):
        try:
            sign_r = self.session.post(
                'https://www.humblebundle.com/api/v1/user/download/sign',
                data={
                    'machine_name': machine_name,
                    'filename': web_name,
                },
            )
        except Exception:
            logger.error("Failed to get download url for trove product {title}"
                         .format(title=web_name))
            return None

        logger.debug(f"Signed url response {sign_r}")
        if sign_r.json().get('_errors') == 'Unauthorized':
            logger.critical("Your account does not have access to the Trove")
            sys.exit()
        signed_url = sign_r.json()['signed_url']
        logger.debug(f"Signed url {signed_url}")
        return signed_url

    def _process_trove_product(self, title, product):
        for platform, download in product['downloads'].items():
            # Sometimes the name has a dir in it
            # Example is "Broken Sword 5 - the Serpent's Curse"
            # Only the Windows file has a dir like
            # "revolutionsoftware/BS5_v2.2.1-win32.zip"
            if self._should_download_platform(platform) is False:  # noqa: E501
                logger.info(f"Skipping {platform} for {title}")
                continue

            web_name = download['url']['web'].split('/')[-1]
            ext = web_name.split('.')[-1]
            if self._should_download_file_type(ext) is False:
                logger.info("Skipping the file {web_name}".format(web_name=web_name))
                continue

            file_info = {
                'uploaded_at': (download.get('uploaded_at')
                                or download.get('timestamp')
                                or product.get('date_added', '0')),
                'md5': download.get('md5', 'UNKNOWN_MD5'),
            }

            cache_file_info: CsvCacheData = self.cache_data_csv.get_cache_item("trove", web_name, trove=True,)

            # cache_file_info: CsvCacheData = CsvCacheData()
            # = self.cache_data.get(cache_file_key, {})

            if cache_file_info in self.cache_data_csv and self.update is not True:
                # Do not care about checking for updates at this time
                continue

            if file_info['uploaded_at'] != cache_file_info['remote_modified_date'] \
                    and file_info['md5'] != cache_file_info['md5']:
                cache_file_info.set_remote_modified_date(file_info['uploaded_at'])
                cache_file_info.set_md5(file_info['md5'])
                product_folder = file_ops.create_product_folder("Humble Trove", title)

                local_filepath = os.path.join(
                    str(product_folder),
                    web_name,
                )
                signed_url = self._get_trove_download_url(
                    download['machine_name'],
                    web_name,
                )
                if signed_url is None:
                    # Failed to get signed url. Error logged in fn
                    continue

                try:
                    product_r = self.session.get(signed_url, stream=True)
                except Exception:
                    logger.error(f"Failed to get trove product {web_name}")
                    continue

                if 'remote_modified_date' in cache_file_info:
                    uploaded_at = time.strftime(
                        '%Y-%m-%d',
                        time.localtime(int(cache_file_info['remote_modified_date']))
                    )
                else:
                    uploaded_at = None

                self._process_download(product_r, cache_file_info, local_filepath, rename_date_str=uploaded_at)

    def _get_trove_products(self):
        trove_products = []
        idx = 0
        trove_base_url = "https://www.humblebundle.com/client/catalog?index={idx}"   # noqa: E501
        while True:
            logger.debug("Collecting trove product data from api pg:{idx} ..."
                         .format(idx=idx))
            trove_page_url = trove_base_url.format(idx=idx)
            try:
                trove_r = self.session.get(trove_page_url)
            except Exception:
                logger.error("Failed to get products from Humble Trove")
                return []

            page_content = trove_r.json()

            if len(page_content) == 0:
                break

            trove_products.extend(page_content)
            idx += 1

        return trove_products

    def _process_order_id(self, order_id, multiprocess_queue: multiprocessing.JoinableQueue):
        order_url = 'https://www.humblebundle.com/api/v1/order/{order_id}?all_tpkds=true'.format(order_id=order_id)
        try:
            order_r = self.session.get(
                order_url,
                headers={
                    'content-type': 'application/json',
                    'content-encoding': 'gzip',
                },
            )
        except Exception as e:
            logger.error("Failed to get order key {order_id}"
                         .format(order_id=order_id))
            return

        logger.debug("Order request: {order_r}".format(order_r=order_r))
        order = order_r.json()
        bundle_title = _clean_name(order['product']['human_name'])
        logger.info("Checking bundle: " + str(bundle_title))
        for product in order['subproducts']:
            self._process_product(order_id, bundle_title, product, multiprocess_queue)

    def _process_product(self, order_id, bundle_title, product, multiprocess_queue: multiprocessing.Queue):
        product_title = _clean_name(product['human_name'])
        # Get all types of download for a product
        for download_type in product['downloads']:
            if self._should_download_platform(download_type['platform']) is False:  # noqa: E501
                logger.info("Skipping {platform} for {product_title}"
                            .format(platform=download_type['platform'], product_title=product_title)
                            )
                continue

            product_folder = file_ops.create_product_folder(bundle_title, product_title)

            # Download each filetype of a product
            for file_type in download_type['download_struct']:
                for content_type in self.content_types:
                    try:
                        url = file_type['url'][content_type]
                    except KeyError:
                        if file_type.get("human_size") != "0 bytes":
                            logger.info("No url found: {bundle_title}/{product_title}"
                                        .format(bundle_title=bundle_title,
                                                product_title=product_title))
                        continue

                    url_filename = url.split('?')[0].split('/')[-1]

                    ext = url_filename.split('.')[-1]
                    if self._should_download_file_type(ext) is False:
                        logger.info("Skipping the file {url_filename}".format(url_filename=url_filename))
                        continue

                    local_filename = os.path.join(product_folder, url_filename)
                    cache_file_info: CsvCacheData = self.cache_data_csv.get_cache_item(order_id, url_filename)

                    if cache_file_info in self.cache_data_csv and self.update is False:
                        # We have the file, and don't want to update.
                        continue
                    cache_file_info.set_md5(file_type.get("md5"))

                    try:
                        product_r = self.session.get(url, stream=True)
                    except Exception:
                        logger.error("Failed to download {url}".format(url=url))
                        continue

                    # Check to see if the file still exists
                    if product_r.status_code != 200:
                        logger.debug(f"File missing for {bundle_title}/{product_title}: {url}")
                        continue

                    logger.debug(f"Item request: {product_r}, Url: {url}")

                    if product_r.headers['Last-Modified'] != cache_file_info['remote_modified_date']:  # noqa: E501
                        if 'remote_modified_date' in cache_file_info:
                            last_modified = datetime.datetime.strptime(
                                cache_file_info['remote_modified_date'],
                                '%a, %d %b %Y %H:%M:%S %Z'
                            ).strftime('%Y-%m-%d')
                        else:
                            last_modified = None
                        cache_file_info.set_remote_modified_date(product_r.headers['Last-Modified'])
                        self._process_download(
                            product_r,
                            cache_file_info,
                            local_filename,
                            rename_date_str=last_modified,
                            multiprocess_queue=multiprocess_queue
                        )

    def _process_download(self, open_r, cache_data: CsvCacheData, local_filename, rename_date_str=None,
                          multiprocess_queue=None):
        try:
            if rename_date_str:
                file_ops.rename_old_file(local_filename, rename_date_str)

            file_ops.download_file(open_r, local_filename, self.progress_bar)

        except (Exception, KeyboardInterrupt) as e:
            if self.progress_bar:
                # Do not overwrite the progress bar on next print
                print()
            logger.error("Failed to download file {local_filename}"
                         .format(local_filename=os.path.basename(local_filename)))

            # Clean up broken downloaded file
            try:
                os.remove(local_filename)  # noqa: E701
            except OSError:
                pass  # noqa: E701

            if type(e).__name__ == 'KeyboardInterrupt':
                sys.exit()

        else:
            cache_data.set_local_modified_date(
                datetime.datetime.now().strftime("%d %b %Y %H:%M:%S %Z")
            )
            if self.progress_bar:
                # Do not overwrite the progress bar on next print
                print()
            multiprocess_queue.put(cache_data)

        finally:
            # Since it's a stream connection, make sure to close it
            open_r.connection.close()

    def _get_purchase_keys(self):
        try:
            library_r = self.session.get('https://www.humblebundle.com/home/library')  # noqa: E501
        except Exception:
            logger.exception("Failed to get list of purchases")
            return []

        logger.debug("Library request: " + str(library_r))
        library_page = parsel.Selector(text=library_r.text)
        user_data = library_page.css('#user-home-json-data').xpath('string()').extract_first()  # noqa: E501
        if user_data is None:
            raise InvalidCookieException()
        orders_json = json.loads(user_data)
        return orders_json['gamekeys']

    def _should_download_platform(self, platform):
        platform = platform.lower()
        if self.platform_include and platform not in self.platform_include:
            return False
        return True

    def _should_download_file_type(self, ext):
        ext = ext.lower()
        if self.ext_include:
            return ext in self.ext_include
        elif self.ext_exclude:
            return ext not in self.ext_exclude
        return True
