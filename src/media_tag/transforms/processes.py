"""Windows job lifetime: descendants stop before the executor releases its lease."""
import os
import time
from media_tag.models.contracts import ModelFailure


class ProcessTree:
    def __init__(self):
        if os.name != 'nt':
            raise ModelFailure('redaction_requires_windows_job_objects')
        import ctypes as c
        from ctypes import wintypes as w
        self.c=c
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),('min_set',c.c_size_t),
                      ('max_set',c.c_size_t),('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(c.Structure):
            _fields_=[(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        class Accounting(c.Structure):
            _fields_=[(n,c.c_int64) for n in ('user','kernel','period_user','period_kernel')]+[(n,w.DWORD) for n in ('faults','total','active','terminated')]
        self.Accounting=Accounting
        k=c.WinDLL('kernel32',use_last_error=True); self.kernel=k
        k.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR]; k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]; k.SetInformationJobObject.restype=w.BOOL
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]; k.AssignProcessToJobObject.restype=w.BOOL
        k.QueryInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]; k.QueryInformationJobObject.restype=w.BOOL
        k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]; k.TerminateJobObject.restype=w.BOOL
        k.CloseHandle.argtypes=[w.HANDLE]
        self.handle=k.CreateJobObjectW(None,None); info=Extended(); info.basic.flags=0x2000
        if not self.handle or not k.SetInformationJobObject(self.handle,9,c.byref(info),c.sizeof(info)):
            if self.handle: k.CloseHandle(self.handle)
            raise ModelFailure('process_tree_containment_failed')

    def assign(self,process):
        if not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise ModelFailure('process_tree_containment_failed')

    def stop_and_close(self):
        self.kernel.TerminateJobObject(self.handle,3)
        info=self.Accounting()
        # Retain quota until the OS reports no active members, including FFmpeg grandchildren.
        while True:
            known=self.kernel.QueryInformationJobObject(self.handle,1,self.c.byref(info),self.c.sizeof(info),None)
            if known and info.active==0: break
            # An unavailable OS state is not evidence that resources stopped. Keep the job/lease held.
            time.sleep(.01)
        self.kernel.CloseHandle(self.handle)
