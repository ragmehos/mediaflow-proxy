import base64
import logging
from urllib.parse import urlparse

import httpx
from fastapi import Request, Response, HTTPException
from starlette.background import BackgroundTask

from .configs import settings
from .const import SUPPORTED_RESPONSE_HEADERS
from .mpd_processor import process_manifest, process_playlist, process_segment
from .schemas import HLSManifestParams, ProxyStreamParams, MPDManifestParams, MPDPlaylistParams, MPDSegmentParams
from .utils.cache_utils import get_cached_mpd, get_cached_init_segment
from .utils.http_utils import (
    Streamer,
    DownloadError,
    download_file_with_retry,
    request_with_retry,
    EnhancedStreamingResponse,
    ProxyRequestHeaders,
)
from .utils.m3u8_processor import M3U8Processor
from .utils.mpd_utils import pad_base64

logger = logging.getLogger(__name__)


async def setup_client_and_streamer(use_request_proxy: bool, verify_ssl: bool) -> tuple[httpx.AsyncClient, Streamer]:
    """
    Set up an HTTP client and a streamer.

    Args:
        use_request_proxy (bool): Whether to use a proxy for the request.
        verify_ssl (bool): Whether to verify SSL certificates.

    Returns:
        tuple: An httpx.AsyncClient instance and a Streamer instance.
    """
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        proxy=settings.proxy_url if use_request_proxy else None,
        verify=verify_ssl,
    )
    return client, Streamer(client)


def handle_exceptions(exception: Exception) -> Response:
    """
    Handle exceptions and return appropriate HTTP responses.

    Args:
        exception (Exception): The exception that was raised.

    Returns:
        Response: An HTTP response corresponding to the exception type.
    """
    if isinstance(exception, httpx.HTTPStatusError):
        logger.error(f"Upstream service error while handling request: {exception}")
        return Response(status_code=exception.response.status_code, content=f"Upstream service error: {exception}")
    elif isinstance(exception, DownloadError):
        logger.error(f"Error downloading content: {exception}")
        return Response(status_code=exception.status_code, content=str(exception))
    else:
        logger.exception(f"Internal server error while handling request: {exception}")
        return Response(status_code=502, content=f"Internal server error: {exception}")


async def handle_hls_stream_proxy(
    request: Request, hls_params: HLSManifestParams, proxy_headers: ProxyRequestHeaders
) -> Response:
    """
    Handle HLS stream proxy requests.

    This function processes HLS manifest files and streams content based on the request parameters.

    Args:
        request (Request): The incoming FastAPI request object.
        hls_params (HLSManifestParams): Parameters for the HLS manifest.
        proxy_headers (ProxyRequestHeaders): Headers to be used in the proxy request.

    Returns:
        Union[Response, EnhancedStreamingResponse]: Either a processed m3u8 playlist or a streaming response.
    """
    client, streamer = await setup_client_and_streamer(hls_params.use_request_proxy, hls_params.verify_ssl)

    try:
        if urlparse(hls_params.destination).path.endswith((".m3u", ".m3u8")):
            return await fetch_and_process_m3u8(
                streamer, hls_params.destination, proxy_headers, request, hls_params.key_url
            )

        response = await streamer.head(hls_params.destination, proxy_headers.request)
        if "mpegurl" in response.headers.get("content-type", "").lower():
            return await fetch_and_process_m3u8(
                streamer, hls_params.destination, proxy_headers, request, hls_params.key_url
            )

        content_range = proxy_headers.request.get("range", "bytes=0-")
        if "NaN" in content_range:
            # Handle invalid range requests "bytes=NaN-NaN"
            raise HTTPException(status_code=416, detail="Invalid Range Header")
        proxy_headers.request.update({"range": content_range})
        response_headers = prepare_response_headers(response.headers, proxy_headers.response)

        return EnhancedStreamingResponse(
            streamer.stream_content(hls_params.destination, proxy_headers.request),
            status_code=response.status_code,
            headers=response_headers,
            background=BackgroundTask(streamer.close),
        )
    except Exception as e:
        await client.aclose()
        return handle_exceptions(e)


async def handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: ProxyRequestHeaders,
    verify_ssl: bool = True,
    use_request_proxy: bool = True,
) -> Response:
    """
    Handle general stream requests.

    This function processes both HEAD and GET requests for video streams.

    Args:
        method (str): The HTTP method (e.g., 'GET' or 'HEAD').
        video_url (str): The URL of the video to stream.
        proxy_headers (ProxyRequestHeaders): Headers to be used in the proxy request.
        verify_ssl (bool, optional): Whether to verify SSL certificates. Defaults to True.
        use_request_proxy (bool, optional): Whether to use a proxy for the request. Defaults to True.

    Returns:
        Union[Response, EnhancedStreamingResponse]: Either a HEAD response or a streaming response.
    """
    client, streamer = await setup_client_and_streamer(use_request_proxy, verify_ssl)

    try:
        response = await streamer.head(video_url, proxy_headers.request)
        response_headers = prepare_response_headers(response.headers, proxy_headers.response)

        if method == "HEAD":
            await streamer.close()
            return Response(headers=response_headers, status_code=response.status_code)
        else:
            return EnhancedStreamingResponse(
                streamer.stream_content(video_url, proxy_headers.request),
                headers=response_headers,
                status_code=response.status_code,
                background=BackgroundTask(streamer.close),
            )
    except Exception as e:
        await client.aclose()
        return handle_exceptions(e)


def prepare_response_headers(original_headers, proxy_response_headers) -> dict:
    """
    Prepare response headers for the proxy response.

    This function filters the original headers, ensures proper transfer encoding,
    and merges them with the proxy response headers.

    Args:
        original_headers (httpx.Headers): The original headers from the upstream response.
        proxy_response_headers (dict): Additional headers to be included in the proxy response.

    Returns:
        dict: The prepared headers for the proxy response.
    """
    response_headers = {k: v for k, v in original_headers.multi_items() if k in SUPPORTED_RESPONSE_HEADERS}
    response_headers.update(proxy_response_headers)
    return response_headers


async def proxy_stream(method: str, stream_params: ProxyStreamParams, proxy_headers: ProxyRequestHeaders):
    """
    Proxies the stream request to the given video URL.

    Args:
        method (str): The HTTP method (e.g., GET, HEAD).
        stream_params (ProxyStreamParams): The parameters for the stream request.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.

    Returns:
        Response: The HTTP response with the streamed content.
    """
    return await handle_stream_request(
        method, stream_params.destination, proxy_headers, stream_params.verify_ssl, stream_params.use_request_proxy
    )


async def fetch_and_process_m3u8(
    streamer: Streamer, url: str, proxy_headers: ProxyRequestHeaders, request: Request, key_url: str = None
):
    """
    Fetches and processes the m3u8 playlist, converting it to an HLS playlist.

    Args:
        streamer (Streamer): The HTTP client to use for streaming.
        url (str): The URL of the m3u8 playlist.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.
        request (Request): The incoming HTTP request.
        key_url (str, optional): The HLS Key URL to replace the original key URL. Defaults to None.

    Returns:
        Response: The HTTP response with the processed m3u8 playlist.
    """
    try:
        content = await streamer.get_text(url, proxy_headers.request)
        processor = M3U8Processor(request, key_url)
        processed_content = await processor.process_m3u8(content, str(streamer.response.url))
        response_headers = {"Content-Disposition": "inline", "Accept-Ranges": "none"}
        response_headers.update(proxy_headers.response)
        return Response(
            content=processed_content,
            media_type="application/vnd.apple.mpegurl",
            headers=response_headers,
        )
    except Exception as e:
        return handle_exceptions(e)
    finally:
        await streamer.close()


async def handle_drm_key_data(key_id, key, drm_info):
    """
    Handles the DRM key data, retrieving the key ID and key from the DRM info if not provided.

    Args:
        key_id (str): The DRM key ID.
        key (str): The DRM key.
        drm_info (dict): The DRM information from the MPD manifest.

    Returns:
        tuple: The key ID and key.
    """
    if drm_info and not drm_info.get("isDrmProtected"):
        return None, None

    if not key_id or not key:
        if "keyId" in drm_info and "key" in drm_info:
            key_id = drm_info["keyId"]
            key = drm_info["key"]
        elif "laUrl" in drm_info and "keyId" in drm_info:
            raise HTTPException(status_code=400, detail="LA URL is not supported yet")
        else:
            raise HTTPException(
                status_code=400, detail="Unable to determine key_id and key, and they were not provided"
            )

    return key_id, key


async def get_manifest(
    request: Request,
    manifest_params: MPDManifestParams,
    proxy_headers: ProxyRequestHeaders,
):
    """
    Retrieves and processes the MPD manifest, converting it to an HLS manifest.

    Args:
        request (Request): The incoming HTTP request.
        manifest_params (MPDManifestParams): The parameters for the manifest request.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.

    Returns:
        Response: The HTTP response with the HLS manifest.
    """
    try:
        mpd_dict = await get_cached_mpd(
            manifest_params.destination,
            headers=proxy_headers.request,
            parse_drm=not manifest_params.key_id and not manifest_params.key,
            verify_ssl=manifest_params.verify_ssl,
            use_request_proxy=manifest_params.use_request_proxy,
        )
    except DownloadError as e:
        raise HTTPException(status_code=e.status_code, detail=f"Failed to download MPD: {e.message}")
    drm_info = mpd_dict.get("drmInfo", {})

    if drm_info and not drm_info.get("isDrmProtected"):
        # For non-DRM protected MPD, we still create an HLS manifest
        return await process_manifest(request, mpd_dict, proxy_headers, None, None)

    key_id, key = await handle_drm_key_data(manifest_params.key_id, manifest_params.key, drm_info)

    # check if the provided key_id and key are valid
    if key_id and len(key_id) != 32:
        key_id = base64.urlsafe_b64decode(pad_base64(key_id)).hex()
    if key and len(key) != 32:
        key = base64.urlsafe_b64decode(pad_base64(key)).hex()

    return await process_manifest(request, mpd_dict, proxy_headers, key_id, key)


async def get_playlist(
    request: Request,
    playlist_params: MPDPlaylistParams,
    proxy_headers: ProxyRequestHeaders,
):
    """
    Retrieves and processes the MPD manifest, converting it to an HLS playlist for a specific profile.

    Args:
        request (Request): The incoming HTTP request.
        playlist_params (MPDPlaylistParams): The parameters for the playlist request.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.

    Returns:
        Response: The HTTP response with the HLS playlist.
    """
    mpd_dict = await get_cached_mpd(
        playlist_params.destination,
        headers=proxy_headers.request,
        parse_drm=not playlist_params.key_id and not playlist_params.key,
        parse_segment_profile_id=playlist_params.profile_id,
        verify_ssl=playlist_params.verify_ssl,
        use_request_proxy=playlist_params.use_request_proxy,
    )
    return await process_playlist(request, mpd_dict, playlist_params.profile_id, proxy_headers)


async def get_segment(
    segment_params: MPDSegmentParams,
    proxy_headers: ProxyRequestHeaders,
):
    """
    Retrieves and processes a media segment, decrypting it if necessary.

    Args:
        segment_params (MPDSegmentParams): The parameters for the segment request.
        proxy_headers (ProxyRequestHeaders): The headers to include in the request.

    Returns:
        Response: The HTTP response with the processed segment.
    """
    try:
        init_content = await get_cached_init_segment(
            segment_params.init_url, proxy_headers.request, segment_params.verify_ssl, segment_params.use_request_proxy
        )
        segment_content = await download_file_with_retry(
            segment_params.segment_url,
            proxy_headers.request,
            verify_ssl=segment_params.verify_ssl,
            use_request_proxy=segment_params.use_request_proxy,
        )
    except Exception as e:
        return handle_exceptions(e)

    return await process_segment(
        init_content,
        segment_content,
        segment_params.mime_type,
        proxy_headers,
        segment_params.key_id,
        segment_params.key,
    )


async def get_public_ip(use_request_proxy: bool = True):
    """
    Retrieves the public IP address of the MediaFlow proxy.

    Args:
        use_request_proxy (bool, optional): Whether to use the proxy configuration from the user's MediaFlow config. Defaults to True.

    Returns:
        Response: The HTTP response with the public IP address.
    """
    ip_address_data = await request_with_retry(
        "GET", "https://api.ipify.org?format=json", {}, use_request_proxy=use_request_proxy
    )
    return ip_address_data.json()


async def proxy_endpoint(request: Request, proxy_params: ProxyStreamParams, proxy_headers: ProxyRequestHeaders):
    # Extract request details
    method = request.method
    body = await request.body()
    proxy_headers.request["content-type"]=request.headers["content-type"]
    # Create a httpx client and forward the request
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            proxy_params.destination,
            headers=proxy_headers.request,
            content=body,
        )
    # Return the response from the target URL
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=dict(response.headers)
    )