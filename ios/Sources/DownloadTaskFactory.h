#pragma once
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN
/// Foundation may raise an Objective-C exception before returning a task for
/// malformed opaque resume data. Swift do/catch cannot intercept that path.
FOUNDATION_EXPORT NSURLSessionDownloadTask * _Nullable
XASSDownloadTaskWithResumeData(NSURLSession *session, NSData *resumeData);
NS_ASSUME_NONNULL_END
