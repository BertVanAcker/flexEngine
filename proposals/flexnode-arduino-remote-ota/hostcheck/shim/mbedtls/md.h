// Host shim: the mbedtls 2.28 md API (ABI of the system libmbedcrypto.so.7), as used by otaCommand.cpp.
#pragma once
#include <cstddef>
extern "C" {
typedef enum { MBEDTLS_MD_NONE = 0, MBEDTLS_MD_MD2, MBEDTLS_MD_MD4, MBEDTLS_MD_MD5, MBEDTLS_MD_SHA1,
               MBEDTLS_MD_SHA224, MBEDTLS_MD_SHA256 } mbedtls_md_type_t;
typedef struct mbedtls_md_info_t mbedtls_md_info_t;
typedef struct { const mbedtls_md_info_t* md_info; void* md_ctx; void* hmac_ctx; } mbedtls_md_context_t;
const mbedtls_md_info_t* mbedtls_md_info_from_type(mbedtls_md_type_t md_type);
void mbedtls_md_init(mbedtls_md_context_t* ctx);
void mbedtls_md_free(mbedtls_md_context_t* ctx);
int mbedtls_md_setup(mbedtls_md_context_t* ctx, const mbedtls_md_info_t* md_info, int hmac);
int mbedtls_md_starts(mbedtls_md_context_t* ctx);
int mbedtls_md_update(mbedtls_md_context_t* ctx, const unsigned char* input, size_t ilen);
int mbedtls_md_finish(mbedtls_md_context_t* ctx, unsigned char* output);
int mbedtls_md_hmac_starts(mbedtls_md_context_t* ctx, const unsigned char* key, size_t keylen);
int mbedtls_md_hmac_update(mbedtls_md_context_t* ctx, const unsigned char* input, size_t ilen);
int mbedtls_md_hmac_finish(mbedtls_md_context_t* ctx, unsigned char* output);
}
