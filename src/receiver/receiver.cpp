#include <arpa/inet.h>
#include <cstring>
#include <iostream>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/time.hpp"

struct ScopedSocket {
  int fd;
  ScopedSocket() : fd(socket(AF_INET, SOCK_DGRAM, 0)) {}
  ~ScopedSocket() {
    if (fd >= 0)
      close(fd);
  }
  operator int() const { return fd; }
};

int main(int argc, char **argv) {
  uint16_t port = 49200;

  ScopedSocket sock;
  if (sock < 0) {
    NLL_ERROR("Failed to create socket");
    return 1;
  }

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  addr.sin_addr.s_addr = INADDR_ANY;

  if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
    NLL_ERROR("Bind failed on port %d", port);
    return 1;
  }

  NLL_INFO("Receiver listening on port %d...", port);
  NLL_INFO("Outputting CSV data to stdout...");

  std::cout << "seq,tx_ts,rx_ts,latency_ns\n";

  std::vector<uint8_t> buffer(2048);
  sockaddr_in client_addr{};
  socklen_t client_len = sizeof(client_addr);

  while (true) {
    ssize_t len = recvfrom(sock, buffer.data(), buffer.size(), 0,
                           (struct sockaddr *)&client_addr, &client_len);

    uint64_t rx_time = nll::real_ns();

    if (len < 0) {
      NLL_WARN("recvfrom failed");
      continue;
    }

    if (static_cast<size_t>(len) < sizeof(nll::message_header)) {
      NLL_WARN("Packet too small: %ld bytes", len);
      continue;
    }

    auto *mh = reinterpret_cast<nll::message_header *>(buffer.data());
    mh->to_host();

    if (mh->magic != 0x6584) {
      NLL_WARN("Invalid Magic: %x", mh->magic);
      continue;
    }

    int64_t latency = rx_time - mh->send_unix_ns;

    std::cout << mh->seq_idx << "," << mh->send_unix_ns << "," << rx_time << ","
              << latency << "\n";

    if (mh->seq_idx % 1000 == 0) {
      NLL_DEBUG("processed seq=%d latency=%ldns", mh->seq_idx, latency);
    }
  }

  return 0;
}
