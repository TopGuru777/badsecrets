import re
import hmac
import struct
import base64
import hashlib
import binascii
from Crypto.Cipher import AES
from Crypto.Cipher import DES
from Crypto.Cipher import DES3
from viewstate import ViewState
from contextlib import suppress
from urllib.parse import urlsplit, urlparse
from badsecrets.helpers import unpad, sp800_108_derivekey, sp800_108_get_key_derivation_parameters
from viewstate.exceptions import ViewStateException
from badsecrets.base import BadsecretsBase, generic_base64_regex


class ASPNET_Viewstate(BadsecretsBase):
    check_secret_args = 3
    identify_regex = generic_base64_regex
    description = {"product": "ASP.NET Viewstate", "secret": "ASP.NET MachineKey"}

    def carve_regex(self):
        return re.compile(
            r"<input.+__VIEWSTATE\"\svalue=\"(.+)\"[\S\s]+<input.+__VIEWSTATEGENERATOR\"\svalue=\"(\w+)\""
        )

    def carve_to_check_secret(self, s, url=None):
        if len(s.groups()) == 2:
            r = self.check_secret(s.groups()[0], s.groups()[1], url)
            return r

    @staticmethod
    def valid_preamble(sourcebytes):
        if sourcebytes[0:2] == b"\xff\x01":
            return True
        return False

    def viewstate_decrypt(self, ekey_bytes, hash_alg, viewstate_B64, url):
        viewstate_bytes = base64.b64decode(viewstate_B64)

        vs_size = len(viewstate_bytes)
        dec_algos = set()
        hash_size = self.hash_sizes[hash_alg]

        if (vs_size - hash_size) % AES.block_size == 0:
            dec_algos.add("AES")
        if (vs_size - hash_size) % DES.block_size == 0:
            dec_algos.add("DES")
            dec_algos.add("3DES")
        for dec_algo in list(dec_algos):
            #       with suppress(ValueError):
            if dec_algo == "AES":
                block_size = AES.block_size
                iv = viewstate_bytes[0:block_size]
                if hash_alg == "_SHA512DOTNET45":
                    s = Simulate_dotnet45_kdf_context_parameters(url)
                    label, context = sp800_108_get_key_derivation_parameters(
                        "WebForms.HiddenFieldPageStatePersister.ClientState", s.get_specific_purposes()
                    )
                    ekey_bytes = sp800_108_derivekey(ekey_bytes, label, context, (len(ekey_bytes) * 8))
                cipher = AES.new(ekey_bytes, AES.MODE_CBC, iv)
                blockpadlen_raw = len(ekey_bytes) % AES.block_size
                if blockpadlen_raw == 0:
                    blockpadlen = block_size
                else:
                    blockpadlen = blockpadlen_raw

            elif dec_algo == "3DES":
                block_size = DES3.block_size
                iv = viewstate_bytes[0:block_size]
                cipher = DES3.new(ekey_bytes[:24], DES3.MODE_CBC, iv)
                blockpadlen = 16

            elif dec_algo == "DES":
                block_size = DES.block_size
                iv = viewstate_bytes[0:block_size]
                cipher = DES.new(ekey_bytes[:8], DES.MODE_CBC, iv)
                blockpadlen = 0

            encrypted_raw = viewstate_bytes[block_size:-hash_size]
            decrypted_raw = cipher.decrypt(encrypted_raw)

            with suppress(TypeError):
                if hash_alg == "_SHA512DOTNET45":
                    decrypt = unpad(decrypted_raw)
                else:
                    decrypt = unpad(decrypted_raw[blockpadlen:])

                if self.valid_preamble(decrypt):
                    return dec_algo
                else:
                    continue

    def viewstate_validate(self, vkey_bytes, encrypted, viewstate_B64, generator, url):
        viewstate_bytes = base64.b64decode(viewstate_B64)

        if encrypted:
            candidate_hash_algs = list(self.hash_sizes.keys())

        else:
            vs = ViewState(viewstate_B64)
            try:
                vs.decode()
            except ViewStateException:
                return None
            signature_len = len(vs.signature)
            candidate_hash_algs = self.search_dict(self.hash_sizes, signature_len)

        for hash_alg in candidate_hash_algs:
            viewstate_data = viewstate_bytes[: -self.hash_sizes[hash_alg]]
            signature = viewstate_bytes[-self.hash_sizes[hash_alg] :]
            if hash_alg == "MD5":
                md5_bytes = viewstate_data + vkey_bytes
                if not encrypted:
                    md5_bytes += b"\x00" * 4
                h = hashlib.md5(md5_bytes)
            else:
                try:
                    vs_data_bytes = viewstate_data
                    if not encrypted:
                        vs_data_bytes += generator

                    if hash_alg == "_SHA512DOTNET45" and url:
                        s = Simulate_dotnet45_kdf_context_parameters(url)
                        label, context = sp800_108_get_key_derivation_parameters(
                            "WebForms.HiddenFieldPageStatePersister.ClientState", s.get_specific_purposes()
                        )
                        vkey_bytes = sp800_108_derivekey(vkey_bytes, label, context, 512)

                    h = hmac.new(
                        vkey_bytes,
                        vs_data_bytes,
                        self.hash_algs[hash_alg],
                    )
                except binascii.Error:
                    continue
            if h.digest() == signature:
                return hash_alg

        return None

    def resolve_args(self, args):
        url_pattern = re.compile(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+")
        generator_pattern = re.compile(r"^[A-F0-9]{8}$")

        url = None
        generator = "0000"

        for arg in args:
            if arg:
                if generator_pattern.match(arg):
                    generator = arg
                elif url_pattern.match(arg):
                    url = arg

        # Remove query string from the URL, if any
        if url:
            url = urlsplit(url)._replace(query="").geturl()
        return generator, url

    def check_secret(self, viewstate_B64, *args):
        generator, url = self.resolve_args(args)

        if not self.identify(viewstate_B64):
            return None

        generator = struct.pack("<I", int(generator, 16))
        if self.valid_preamble(base64.b64decode(viewstate_B64)):
            encrypted = False
        else:
            encrypted = True

        for l in self.load_resources(["aspnet_machinekeys.txt"]):
            try:
                vkey, ekey = l.rstrip().split(",")
            except ValueError:
                continue
            with suppress(ValueError):
                validationAlgo = self.viewstate_validate(
                    binascii.unhexlify(vkey), encrypted, viewstate_B64, generator, url
                )
                if validationAlgo:
                    confirmed_ekey = None
                    decryptionAlgo = None
                    if encrypted:
                        with suppress(binascii.Error):
                            ekey_bytes = binascii.unhexlify(ekey)
                            decryptionAlgo = self.viewstate_decrypt(ekey_bytes, validationAlgo, viewstate_B64, url)
                            if decryptionAlgo:
                                confirmed_ekey = ekey

                    result = f"validationKey: {vkey} validationAlgo: {validationAlgo}"
                    if confirmed_ekey:
                        result += f" encryptionKey: {confirmed_ekey} encryptionAlgo: {decryptionAlgo}"
                    return {"secret": result, "details": None}
        return None


# Based on https://github.com/pwntester/ysoserial.net/blob/master/ysoserial/Plugins/ViewStatePlugin.cs and translated to python. All credit to ysoserial.net.
class Simulate_dotnet45_kdf_context_parameters:
    def __init__(self, url):
        self.url = url

    def simulate_template_source_directory(self, str_path):
        if not str_path.startswith("/"):
            str_path = "/" + str_path
        if "." in str_path.split("/")[-1]:
            # File name needs to be removed
            str_path = str_path.rsplit("/", 1)[0] + "/"
        str_path = self.remove_slash_from_path_if_needed(str_path)
        if str_path == "":
            return "/"
        return str_path

    @staticmethod
    def remove_slash_from_path_if_needed(path):
        if path and path.endswith("/"):
            path = path[:-1]
        return path

    def simulate_get_type_name(self, str_path, iis_app_in_path):
        if not str_path.startswith("/"):
            str_path = "/" + str_path
        if not str_path.lower().endswith(".aspx"):
            str_path += "/default.aspx"
        iis_app_in_path = iis_app_in_path.lower()
        if not iis_app_in_path.startswith("/"):
            iis_app_in_path = "/" + iis_app_in_path
        if not iis_app_in_path.endswith("/"):
            iis_app_in_path += "/"
        if iis_app_in_path in str_path.lower():
            str_path = str_path.lower().split(iis_app_in_path, 1)[1]
        if str_path.startswith("/"):
            str_path = str_path[1:]
        str_path = str_path.replace(".", "_").replace("/", "_")
        str_path = self.remove_slash_from_path_if_needed(str_path)
        return str_path

    @staticmethod
    def extract_from_url(url):
        parsed_url = urlparse(url)
        str_path = parsed_url.path
        iis_app_in_path = str_path.rsplit("/", 1)[0] or "/"
        return str_path, iis_app_in_path

    def get_specific_purposes(self):
        str_path, iis_app_in_path = self.extract_from_url(self.url)
        template_source = self.simulate_template_source_directory(iis_app_in_path)
        gettype = self.simulate_get_type_name(str_path, iis_app_in_path)
        specificPurposes = []
        specificPurposes.append(f"TemplateSourceDirectory: {template_source.upper()}")
        specificPurposes.append(f"Type: {gettype.upper()}")
        return specificPurposes
