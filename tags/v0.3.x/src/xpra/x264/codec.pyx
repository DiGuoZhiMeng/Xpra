# This file is part of Parti.
# Copyright (C) 2012 Antoine Martin <antoine@devloop.org.uk>
# Parti is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#gcc -pthread -shared -O0 build/temp.linux-x86_64-2.7/xpra/x264/codec.o xpra/x264/x264lib.o -L/usr/lib64 -lx264 -lavcodec -lswscale -lpthread -lpython2.7 -o build/lib.linux-x86_64-2.7/xpra/x264/codec.so

from libc.stdlib cimport free

cdef extern from "Python.h":
    ctypedef int Py_ssize_t
    ctypedef object PyObject
    int PyObject_AsReadBuffer(object obj,
                              void ** buffer,
                              Py_ssize_t * buffer_len) except -1

ctypedef unsigned char uint8_t
ctypedef void x264lib_ctx
cdef extern from "x264lib.h":
    x264lib_ctx* init_encoder(int width, int height)
    void clean_encoder(x264lib_ctx *context)
    int compress_image(x264lib_ctx *context, uint8_t *input, int stride, uint8_t **out, int *outsz)
    x264lib_ctx* init_decoder(int width, int height)
    void clean_decoder(x264lib_ctx *context)
    int decompress_image(x264lib_ctx *context, uint8_t *input, int size, uint8_t **out, int *outsize, int *outstride)
    void change_encoding_speed(x264lib_ctx *context, int increase)


ENCODERS = {}
DECODERS = {}


""" common superclass for Decoder and Encoder """
cdef class xcoder:
    cdef x264lib_ctx *context
    cdef int width
    cdef int height

    def init(self, width, height):
        self.init_context(width, height)
        assert self.context, "failed to initialize context"
        self.width = width
        self.height = height

    def __dealloc__(self):
        self.clean()

    def get_width(self):
        return self.width

    def get_height(self):
        return self.height

    def init_context(self, width, height):
        self.context = NULL


cdef class Decoder(xcoder):
    cdef uint8_t *last_image

    def init_context(self, width, height):
        self.context = init_decoder(width, height)
    
    def clean(self):
        if self.context!=NULL:
            clean_decoder(self.context)
            free(self.context)
            self.context = NULL

    def decompress_image(self, input):
        cdef uint8_t *dout
        cdef int outsize
        cdef int outstride
        cdef unsigned char * buf = <uint8_t *> 0
        cdef Py_ssize_t buf_len = 0
        assert self.context!=NULL
        assert self.last_image==NULL
        PyObject_AsReadBuffer(input, <void **>&buf, &buf_len)
        i = decompress_image(self.context, buf, buf_len, &dout, &outsize, &outstride)
        self.last_image = dout
        if i!=0:
            return i, 0, ""
        doutv = (<char *>dout)[:outsize]
        return  i, outstride, doutv

    def free_image(self):
        assert self.last_image!=NULL
        free(self.last_image)
        self.last_image = NULL


cdef class Encoder(xcoder):

    def init_context(self, width, height):
        self.context = init_encoder(width, height)
    
    def clean(self):
        if self.context!=NULL:
            clean_encoder(self.context)
            self.context = NULL

    def compress_image(self, input, rowstride):
        cdef uint8_t *cout
        cdef int coutsz
        cdef uint8_t *buf = <uint8_t *> 0
        cdef Py_ssize_t buf_len = 0
        assert self.context!=NULL
        PyObject_AsReadBuffer(input, <void **>&buf, &buf_len)
        i = compress_image(self.context, buf, rowstride, &cout, &coutsz)
        if i!=0:
            return i, 0, ""
        coutv = (<char *>cout)[:coutsz]
        return  i, coutsz, coutv

    def increase_encoding_speed(self):
        change_encoding_speed(self.context, 1)
    
    def decrease_encoding_speed(self):
        change_encoding_speed(self.context, -1)
