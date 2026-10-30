#import "DownloadTaskFactory.h"

NSURLSessionDownloadTask * _Nullable
XASSDownloadTaskWithResumeData(NSURLSession *session, NSData *resumeData) {
    @try {
        // Treat the data as opaque: never repair, deserialize, or rewrite it.
        return [session downloadTaskWithResumeData:resumeData];
    } @catch (NSException *exception) {
        // This boundary covers only task construction. The Swift coordinator
        // must persist its one-shot budget before starting any replacement.
        // Do not log the exception: its contents can contain private tickets.
        return nil;
    }
}
