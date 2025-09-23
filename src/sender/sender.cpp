#include <sys/socket.h> // For socket(), bind(), sendto(), recvfrom()
#include <netinet/in.h> // For sockaddr_in, htons(), htonl()
#include <arpa/inet.h>  // For inet_addr()
#include <unistd.h>     // For close()
#include <cstring>      // For memset()

#include "common/packet.hpp"
#include "common/log.hpp"
int main(int argc, char** argv) {

    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
    }

    return 0;
}