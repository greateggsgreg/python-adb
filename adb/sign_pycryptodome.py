from adb import adb_protocol

from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Util import number


class PycryptodomeAuthSigner(adb_protocol.AuthSigner):

    def __init__(self, rsa_key_path=None):
        super(PycryptodomeAuthSigner, self).__init__()

        if rsa_key_path:
            with open(rsa_key_path + '.pub', 'rb') as rsa_pub_file:
                self.public_key = rsa_pub_file.read()

            with open(rsa_key_path, 'rb') as rsa_priv_file:
                self.rsa_key = RSA.import_key(rsa_priv_file.read())

    def Sign(self, data):
        # Prepend precomputed ASN1 hash code for SHA1
        data = b'\x30\x21\x30\x09\x06\x05\x2b\x0e\x03\x02\x1a\x05\x00\x04\x14' + data
        pkcs = pkcs1_15.new(self.rsa_key)

        # See 8.2.1 in RFC3447
        modBits = number.size(pkcs._key.n)
        k = pkcs1_15.ceil_div(modBits,8) # Convert from bits to bytes

        # Step 2a (OS2IP)
        em_int = pkcs1_15.bytes_to_long(PycryptodomeAuthSigner._pad_for_signing(data, k))
        # Step 2b (RSASP1)
        m_int = pkcs._key._decrypt(em_int)
        # Step 2c (I2OSP)
        signature = pkcs1_15.long_to_bytes(m_int, k)

        return signature

    def GetPublicKey(self):
        return self.public_key

    @staticmethod
    def _pad_for_signing(message, target_length):
        """Pads the message for signing, returning the padded message.

        The padding is always a repetition of FF bytes.

        Function from python-rsa to replace _EMSA_PKCS1_V1_5_ENCODE's for our use case

        :return: 00 01 PADDING 00 MESSAGE

        """

        max_msglength = target_length - 11
        msglength = len(message)

        if msglength > max_msglength:
            raise OverflowError('%i bytes needed for message, but there is only'
                                ' space for %i' % (msglength, max_msglength))

        padding_length = target_length - msglength - 3

        return b''.join([b'\x00\x01',
                         padding_length * b'\xff',
                         b'\x00',
                         message])
