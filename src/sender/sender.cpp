#include <sys/socket.h> // For socket(), bind(), sendto(), recvfrom()
#include <netinet/in.h> // For sockaddr_in, htons(), htonl()
#include <arpa/inet.h>  // For inet_addr()
#include <unistd.h>     // For close()
#include <cstring>      // For memset()
#include <errno.h>
#include <endian.h>
#include "common/packet.hpp"
#include "common/log.hpp"
using nll::log::logf;
int main(int argc, char** argv) {

    int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) {
	    logf(0, "Error creating socket in sender.cpp\n.");
    }
    
    struct sockaddr_in dest_addr;
    dest_addr.sin_family = AF_INET;
    dest_addr.sin_port = htons(49200);
    dest_addr.sin_addr.s_addr = inet_addr("192.168.1.11");

    struct nll::message_header mh;

    mh.magic = htons(0x6984); // "ET"
    mh.version = 1;
    mh.msg_type = 0;
    for(uint32_t i = 0; i < 10; ++i) {
	    mh.seq_idx = htonl(i);
	    mh.send_unix_ns = htobe64(static_cast<uint64_t>(nll::real_ns().count()));
		
	    ssize_t bytes_sent = sendto(sockfd,
			   		 (void*)&mh,
					 sizeof(mh),
					 0,
					 (const struct sockaddr *)&dest_addr,
					 sizeof(dest_addr)
			   		 );
	    if (bytes_sent == -1) logf(0, "Error: %s\n", strerror(errno));


    }
    return 0;
}
