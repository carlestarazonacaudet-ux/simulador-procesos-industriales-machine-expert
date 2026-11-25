"""Cliente Modbus TCP para operaciones individuales de memoria."""

import re
import socket
import struct
from dataclasses import dataclass


class ProtocolError(Exception):
    pass


class ModbusException(ProtocolError):
    def __init__(self, function, code):
        self.code = code
        meanings = {
            1: "función no soportada",
            2: "dirección no disponible",
            3: "valor rechazado",
            4: "fallo del servidor",
            11: "el dispositivo de destino no responde",
        }
        super().__init__(f"Modbus FC{function}, excepción {code}: "
                         f"{meanings.get(code, 'error del servidor')}.")


@dataclass(frozen=True)
class Address:
    index: int
    coil: bool = False
    bit: int | None = None


def parse_address(value):
    match = re.fullmatch(r"%?(MW|MX|M)([0-9]{1,6})(?:\.([0-9]{1,2}))?",
                         value.strip().upper())
    if not match:
        raise ValueError("Usa MW100, MX200.0, MW100.0 o M10, con o sin % inicial.")
    area, position, suffix = match.groups()
    index = int(position)
    bit = int(suffix) if suffix is not None else None
    if area == "MX":
        if bit is None or bit > 7:
            raise ValueError("%MX requiere un bit de 0 a 7.")
        index, byte_in_word = divmod(index, 2)
        bit += 8 * byte_in_word
    elif area == "MW" and bit is not None and bit > 15:
        raise ValueError("%MW admite bits de 0 a 15.")
    elif area == "M" and bit is not None:
        raise ValueError("%M usa un índice sin punto.")
    if index > 65535:
        raise ValueError("La dirección Modbus debe estar entre 0 y 65535.")
    return Address(index, area == "M", bit)


def receive_exact(connection, count):
    data = bytearray()
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ProtocolError("El servidor cerró la conexión antes de responder.")
        data.extend(chunk)
    return bytes(data)


class ModbusClient:
    def __init__(self, connection, unit):
        self.connection = connection
        self.unit = unit
        self.transaction = 0

    def request(self, function, *words):
        self.transaction = (self.transaction + 1) % 65536
        pdu = struct.pack(">B" + "H" * len(words), function, *words)
        header = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, self.unit)
        self.connection.sendall(header + pdu)
        transaction, protocol, length, unit = struct.unpack(
            ">HHHB", receive_exact(self.connection, 7)
        )
        if (transaction, protocol, unit) != (self.transaction, 0, self.unit):
            raise ProtocolError("La cabecera de la respuesta no coincide con la solicitud.")
        if not 2 <= length <= 254:
            raise ProtocolError("Longitud de respuesta Modbus incorrecta.")
        reply = receive_exact(self.connection, length - 1)
        if len(reply) == 2 and reply[0] == (function | 128):
            raise ModbusException(function, reply[1])
        if reply[0] != function:
            raise ProtocolError("Código de función inesperado en la respuesta.")
        return pdu, reply

    def read(self, address):
        function, byte_count = (1, 1) if address.coil else (3, 2)
        _, reply = self.request(function, address.index, 1)
        if len(reply) != byte_count + 2 or reply[1] != byte_count:
            raise ProtocolError("Longitud de datos Modbus incorrecta.")
        value = int.from_bytes(reply[2:], "big")
        if address.coil:
            return value & 1
        if address.bit is not None:
            return (value >> address.bit) & 1
        return value

    def write_echo(self, function, *words):
        request, reply = self.request(function, *words)
        if reply != request:
            raise ProtocolError("El servidor devolvió una confirmación de escritura inesperada.")

    def write(self, address, value, allow_read_modify_write=False):
        if address.coil:
            self.write_echo(5, address.index, 0xFF00 if value else 0)
        elif address.bit is None:
            self.write_echo(6, address.index, value)
        else:
            mask = 1 << address.bit
            replacement = mask if value else 0
            try:
                self.write_echo(22, address.index, 0xFFFF ^ mask, replacement)
            except ModbusException as error:
                if error.code != 1:
                    raise
                if not allow_read_modify_write:
                    raise ProtocolError(
                        "El servidor no admite escritura por máscara. "
                        "Habilita la escritura por lectura y modificación si la palabra "
                        "no se modifica simultáneamente."
                    ) from error
                current = self.read(Address(address.index))
                self.write_echo(6, address.index, (current & ~mask) | replacement)


def exchange(host, port, unit, address, value=None, allow_read_modify_write=False):
    with socket.create_connection((host, port), timeout=3) as connection:
        client = ModbusClient(connection, unit)
        if value is None:
            return client.read(address)
        client.write(address, value, allow_read_modify_write)
        return None
