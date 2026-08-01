#ifndef CWIND_WAL_MANAGER_H
    #define CWIND_WAL_MANAGER_H

    #include "../stl/ess/cwind_fix_size_queue.h"

    typedef struct CWWalManager {
        /**
         * 虽然现在还只是简单的数组, 但是也许以后会有其它操作呢? 总之先用对象了
         */
        CWFSQueue_t *fifo;
        
    } CWWalManager_t;

#endif
