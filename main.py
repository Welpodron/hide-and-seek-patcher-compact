import aiohttp
import asyncio
import os
import hashlib
from pathlib import Path
import json
from Crypto.Cipher import AES
from Crypto.Util import Counter
import base64
import copy
import logging

import config

logging.basicConfig(
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.PATH_LOG, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def encrypt_string(plaintext, key=config.KEY):
    ctr = Counter.new(128)
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
    ciphertext = cipher.encrypt(plaintext)

    return ciphertext


def decrypt_string(ciphertext, key=config.KEY):
    ctr = Counter.new(128)
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
    plaintext = cipher.decrypt(ciphertext)

    return plaintext


def _validate_cache_filter(remote_file, local_files, cache):
    local_github_cache = cache.get("g")
    local_disk_cache = cache.get("d")

    if next(
        (
            local_github_cache_file
            for local_github_cache_file in local_github_cache
            if local_github_cache_file.get("path") == remote_file.get("path")
            and local_github_cache_file.get("sha") == remote_file.get("sha")
        ),
        None,
    ):
        local_file = next(
            (
                local_file
                for local_file in local_files
                if local_file.get("path") == remote_file.get("path")
            ),
            None,
        )

        if local_file and next(
            (
                local_disk_cache_file
                for local_disk_cache_file in local_disk_cache
                if local_disk_cache_file.get("sha") == local_file.get("sha")
            ),
            None,
        ):
            return False

        return True

    return True


def validate_cache(key=config.KEY, local_files=None, remote_files=None):
    logging.info("Проверка кэша...")
    try:
        with open(config.PATH_CACHE, "rb") as file:
            ciphertext = file.read()
            decrypted_text = decrypt_string(key=key, ciphertext=ciphertext).decode(
                "utf-8"
            )
            decrypted_data = json.loads(decrypted_text)

            if (not decrypted_data.get("g")) or (not decrypted_data.get("d")):
                return {"files": remote_files, "cache": {"g": [], "d": []}}

            filtered_remote_files = list(
                filter(
                    lambda remote_file: _validate_cache_filter(
                        remote_file=remote_file,
                        local_files=local_files,
                        cache=decrypted_data,
                    ),
                    remote_files,
                )
            )
            logging.info("Кэш найден")
            return {"files": filtered_remote_files, "cache": decrypted_data}

    except:
        logging.info("Кэш не найден или поврежден")
        return {"files": remote_files, "cache": {"g": [], "d": []}}


def save_cache(
    cache: str,
    key=config.KEY,
):
    with open(config.PATH_CACHE, "wb") as file:
        file.write(encrypt_string(key=key, plaintext=cache.encode("utf-8")))


def get_local_files():
    files = []

    root_directory = Path(config.PATH_BUILD)

    for f in root_directory.glob("**/*"):
        if f.is_file():
            with open(f, "rb") as file:
                file_hash = hashlib.sha1(file.read()).hexdigest()
            file_path = str(f.relative_to(root_directory)).replace("\\", "/")
            files.append({"path": file_path, "sha": file_hash})

    return files


async def get_remote_file(session, url, remote_path, remote_sha, cache):
    async with session.get(url) as response:
        logging.info(f"Загрузка {remote_path}...")
        if response.status != 200:
            logging.error(
                f"Произошла ошибка при загрузке {remote_path}: {response.content}"
            )
            raise Exception("Error:", response.content)
        json = await response.json()

        file_content = base64.b64decode(json.get("content").encode("utf-8"))
        file_hash = hashlib.sha1(file_content).hexdigest()
        save_path = os.path.join(config.PATH_BUILD, remote_path)

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        with open(save_path, "wb") as file:
            file.write(file_content)

        logging.info(f"Файл {remote_path} успешно загружен и сохранен в: {save_path}")

        cache_hit_index = next(
            (
                cache_record_index
                for cache_record_index, cache_record in enumerate(cache.get("g"))
                if cache_record.get("path") == remote_path
            ),
            -1,
        )

        if cache_hit_index == -1:
            cache["g"].append({"path": remote_path, "sha": remote_sha})
            cache["d"].append({"path": remote_path, "sha": file_hash})
        else:
            cache["g"][cache_hit_index] = {"path": remote_path, "sha": remote_sha}
            cache["d"][cache_hit_index] = {"path": remote_path, "sha": file_hash}


async def get_remote_files(session, url):
    async with session.get(url) as response:
        if response.status != 200:
            logging.error(f"Произошла ошибка при загрузке файлов: {response.content}")
            raise Exception("Error:", response.content)
        json = await response.json()

        remote_files = list(
            filter(lambda file: file.get("type") == "blob", json.get("tree"))
        )

        return remote_files


async def sync():
    logging.info("Запущена синхронизация")

    local_files = get_local_files()

    session = aiohttp.ClientSession(headers=config.GITHUB_HEADERS)

    remote_files = await get_remote_files(
        session=session,
        url=f"https://api.github.com/repos/{config.GITHUB_USER}/{config.GITHUB_REPO}/git/trees/HEAD?recursive=1",
    )

    validated_files = validate_cache(local_files=local_files, remote_files=remote_files)
    files_to_download = validated_files.get("files")
    files_cache = copy.deepcopy(validated_files.get("cache"))

    logging.info(f"Загрузка {len(files_to_download)} файлов...")

    tasks = []

    for remote_file in files_to_download:
        url = f"https://api.github.com/repos/{config.GITHUB_USER}/{config.GITHUB_REPO}/git/blobs/{remote_file.get('sha')}"
        tasks.append(
            asyncio.ensure_future(
                get_remote_file(
                    session=session,
                    url=url,
                    remote_path=remote_file.get("path"),
                    remote_sha=remote_file.get("sha"),
                    cache=files_cache,
                )
            )
        )

    await asyncio.gather(*tasks)

    await session.close()
    logging.info("Загрузка файлов завершена")
    logging.info("Сохранение кэша...")

    save_cache(cache=json.dumps(files_cache))

    logging.info("Кэш сохранен")
    logging.info("Синхронизация завершена")
    os.system('pause')


loop = asyncio.get_event_loop()
loop.run_until_complete(sync())
