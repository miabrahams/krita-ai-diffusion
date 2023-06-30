import asyncio
import json
from typing import Callable, NamedTuple, Union
from .image import Extent, Image, ImageCollection
from .settings import settings

from PyQt5.QtCore import QByteArray, QUrl
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply


class NetworkError(Exception):
    def __init__(self, code, msg, url):
        self.code = code
        self.message = msg
        self.url = url
        super().__init__(self, msg)

    def __str__(self):
        return self.message

    @staticmethod
    def from_reply(reply: QNetworkReply):
        code = reply.error()
        url = reply.url().toString()
        try:  # extract detailed information from the payload
            data = json.loads(reply.readAll().data())
            if data.get("error", "") == "OutOfMemoryError":
                msg = data.get("errors", reply.errorString())
                return OutOfMemoryError(code, msg, url)
            detail = data.get("detail", "")
            errors = data.get("errors", "")
            if detail != "" or errors != "":
                return NetworkError(code, f"{detail} {errors} ({reply.errorString()})")
        except:
            pass
        return NetworkError(code, reply.errorString(), url)


class OutOfMemoryError(NetworkError):
    def __init__(self, code, msg, url):
        super().__init__(code, msg, url)


class Interrupted(Exception):
    def __init__(self):
        super().__init__(self, "Operation cancelled")


class Request(NamedTuple):
    url: str
    future: asyncio.Future


class RequestManager:
    def __init__(self):
        self._net = QNetworkAccessManager()
        self._net.finished.connect(self._finished)
        self._requests = {}

    def request(self, method, url: str, data: dict = None):
        self._cleanup()

        request = QNetworkRequest(QUrl(url))
        # request.setTransferTimeout({"GET": 30000, "POST": 0}[method]) # requires Qt 5.15 (Krita 5.2)
        if data is not None:
            data_bytes = QByteArray(json.dumps(data).encode("utf-8"))
            request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
            request.setHeader(QNetworkRequest.ContentLengthHeader, data_bytes.size())

        assert method in ["GET", "POST"]
        if method == "POST":
            reply = self._net.post(request, data_bytes)
        else:
            reply = self._net.get(request)

        future = asyncio.get_running_loop().create_future()
        self._requests[reply] = Request(url, future)
        return future

    def get(self, url: str):
        return self.request("GET", url)

    def post(self, url: str, data: dict):
        return self.request("POST", url, data)

    def _finished(self, reply: QNetworkReply):
        code = reply.error()
        future = self._requests[reply].future
        if future.cancelled():
            return  # operation was cancelled, discard result
        if code == QNetworkReply.NoError:
            future.set_result(json.loads(reply.readAll().data()))
        else:
            future.set_exception(NetworkError.from_reply(reply))

    def _cleanup(self):
        self._requests = {
            reply: request for reply, request in self._requests.items() if not reply.isFinished()
        }


class Progress:
    callback: Callable[[float], None]
    scale: float = 1
    offset: float = 0

    def __init__(self, callback: Callable[[float], None], scale: float = 1):
        self.callback = callback
        self.scale = scale

    @staticmethod
    def forward(other, scale: float = 1):
        return Progress(other.callback, scale)

    def __call__(self, progress: float):
        self.callback(self.offset + self.scale * progress)

    def finish(self):
        self.offset = self.offset + self.scale
        self.callback(self.offset)


def _collect_images(result, count: int = ...):
    if "images" in result:
        images = result["images"]
        assert isinstance(images, list)
        if count is not ...:
            images = images[:count]
        return ImageCollection(map(Image.from_base64, images))
    raise Interrupted()


def _make_tiled_vae_payload():
    return {"tiled vae": {"args": [True, 1536]}}  # TODO hardcoded tile size


class Auto1111:
    default_url = "http://127.0.0.1:7860"
    default_upscaler = "Lanczos"
    default_sampler = "DPM++ 2M Karras"

    _requests = RequestManager()

    url: str
    negative_prompt = "EasyNegative verybadimagenegative_v1.3"
    upscale_prompt = "highres 8k uhd"

    @staticmethod
    async def connect(url=default_url):
        result = Auto1111(url)
        upscalers = await result._get("sdapi/v1/upscalers")
        settings.upscalers = [u["name"] for u in upscalers if not u["name"] == "None"]
        return result

    def __init__(self, url):
        self.url = url

    async def _get(self, op: str):
        return await self._requests.get(f"{self.url}/{op}")

    async def _post(self, op: str, data: dict, progress: Progress = ...):
        request = self._requests.post(f"{self.url}/{op}", data)
        if progress is not ...:
            while not request.done():
                status = await self._get("sdapi/v1/progress")
                if status["progress"] >= 1:
                    break
                elif status["progress"] > 0:
                    progress(status["progress"])
            progress.finish()
        return await request

    async def txt2img(self, prompt: str, extent: Extent, progress: Progress):
        payload = {
            "prompt": prompt,
            "negative_prompt": self.negative_prompt,
            "batch_size": settings.batch_size,
            "steps": 30,
            "cfg_scale": 7,
            "width": extent.width,
            "height": extent.height,
            "sampler_index": self.default_sampler,
        }
        result = await self._post("sdapi/v1/txt2img", payload, progress)
        return _collect_images(result)

    async def txt2img_inpaint(
        self, img: Image, mask: Image, prompt: str, extent: Extent, progress: Progress
    ):
        assert img.extent == mask.extent
        cn_payload = {
            "controlnet": {
                "args": [
                    {
                        "input_image": img.to_base64(),
                        "mask": mask.to_base64(),
                        "module": "inpaint_only+lama",
                        "model": "control_v11p_sd15_inpaint [ebff9138]",
                        "control_mode": "ControlNet is more important",
                        "pixel_perfect": True,
                    }
                ]
            }
        }
        payload = {
            "prompt": prompt,
            "negative_prompt": self.negative_prompt,
            "batch_size": settings.batch_size,
            "steps": 20,
            "cfg_scale": 5,
            "width": extent.width,
            "height": extent.height,
            "alwayson_scripts": cn_payload,
            "sampler_index": "DDIM",
        }
        result = await self._post("sdapi/v1/txt2img", payload, progress)
        return _collect_images(result, count=-1)

    async def _img2img(
        self,
        image: Union[Image, str],
        prompt: str,
        strength: float,
        extent: Extent,
        cfg_scale: int,
        batch_size: int,
        progress: Progress,
    ):
        image = image.to_base64() if isinstance(image, Image) else image
        payload = {
            "init_images": [image],
            "denoising_strength": strength,
            "prompt": prompt,
            "negative_prompt": self.negative_prompt,
            "batch_size": batch_size,
            "steps": 30,
            "cfg_scale": cfg_scale,
            "width": extent.width,
            "height": extent.height,
            "alwayson_scripts": _make_tiled_vae_payload(),
            "sampler_index": self.default_sampler,
        }
        result = await self._post("sdapi/v1/img2img", payload, progress)
        return _collect_images(result)

    async def img2img(
        self, img: Image, prompt: str, strength: float, extent: Extent, progress: Progress
    ):
        return await self._img2img(img, prompt, strength, extent, 7, settings.batch_size, progress)

    async def img2img_inpaint(
        self,
        img: Image,
        mask: Image,
        prompt: str,
        strength: float,
        extent: Extent,
        progress: Progress,
    ):
        assert img.extent == mask.extent
        cn_payload = {
            "controlnet": {
                "args": [
                    {
                        "module": "inpaint_only",
                        "model": "control_v11p_sd15_inpaint [ebff9138]",  # TODO hardcoded ctrlnet
                        "control_mode": "Balanced",
                        "pixel_perfect": True,
                    }
                ]
            }
        }
        payload = {
            "init_images": [img.to_base64()],
            "denoising_strength": strength,
            "mask": mask.to_base64(),
            "mask_blur": 0,
            "inpainting_fill": 1,
            "inpainting_full_res": True,
            "prompt": prompt,
            "negative_prompt": self.negative_prompt,
            "batch_size": settings.batch_size,
            "steps": 30,
            "cfg_scale": 7,
            "width": extent.width,
            "height": extent.height,
            "alwayson_scripts": cn_payload,
            "sampler_index": self.default_sampler,
        }
        result = await self._post("sdapi/v1/img2img", payload, progress)
        return _collect_images(result, count=-1)

    async def upscale(self, img: Image, target: Extent, prompt: str, progress: Progress):
        upscale_payload = {
            "resize_mode": 1,  # width & height
            "upscaling_resize_w": target.width,
            "upscaling_resize_h": target.height,
            "upscaler_1": settings.upscaler,
            "image": img.to_base64(),
        }
        result = await self._post("sdapi/v1/extra-single-image", upscale_payload)
        result = await self._img2img(
            image=result["image"],
            prompt=f"{self.upscale_prompt}, {prompt}",
            strength=0.4,
            extent=target,
            cfg_scale=5,
            batch_size=1,
            progress=progress,
        )
        return result

    async def upscale_tiled(self, img: Image, target: Extent, prompt: str, progress: Progress):
        # TODO dead code, consider multi diffusion
        cn_payload = {
            "controlnet": {
                "args": [
                    {
                        "input_image": img.to_base64(),
                        "module": "tile_resample",
                        "model": "control_v11f1e_sd15_tile [a371b31b]",
                        "control_mode": "Balanced",
                    }
                ]
            }
        }
        upscale_args = [
            None,  # _
            768,  # tile_width
            768,  #  tile_height
            8,  # mask_blur
            32,  # padding
            0,  # seams_fix_width
            0,  # seams_fix_denoise
            0,  # seams_fix_padding
            settings.upscaler_index,
            False,  # save_upscaled_image
            0,  # redraw mode = LINEAR
            False,  # save_seams_fix_image
            0,  # seams_fix_mask_blur
            0,  # seams_fix_type = NONE
            0,  # size type
            0,  # width
            0,  # height
            0,  # scale = FROM_IMG2IMG
        ]
        payload = {
            "init_images": [img.to_base64()],
            "resize_mode": 0,
            "denoising_strength": 0.4,
            "prompt": f"{self.upscale_prompt}, {prompt}",
            "negative_prompt": self.negative_prompt,
            "sampler_index": "DPM++ 2M Karras",
            "steps": 30,
            "cfg_scale": 5,
            "width": target.width,
            "height": target.height,
            "script_name": "ultimate sd upscale",
            "script_args": upscale_args,
            "alwayson_scripts": cn_payload,
        }
        result = await self._post("sdapi/v1/img2img", payload, progress)
        return _collect_images(result)

    async def interrupt(self):
        return await self._post("sdapi/v1/interrupt", {})
