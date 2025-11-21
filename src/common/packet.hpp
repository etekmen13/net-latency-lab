#pragma once
#include <cstdint>
#include <endian.h>

namespace nll {

struct __attribute__((packed)) message_header {
  uint16_t magic; // 0x6584
  uint8_t version;
  uint8_t msg_type;
  uint32_t seq_idx;
  uint64_t send_unix_ns; // CLOCK_REALTIME

  void to_network() {
    magic = htobe16(magic);
    seq_idx = htobe32(seq_idx);
    send_unix_ns = htobe64(send_unix_ns);
  }

  void to_host() {
    magic = be16toh(magic);
    seq_idx = be32toh(seq_idx);
    send_unix_ns = be64toh(send_unix_ns);
  }
};

enum class payload { TINY = 16, SMALL = 256, MEDIUM = 1024 };

} // namespace nll
