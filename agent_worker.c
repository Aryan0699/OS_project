// Read-only agent tools. All requests run in this same process, so future
// process taint can follow a session. No new system calls are needed here.
#include "types.h"
#include "stat.h"
#include "user.h"
#include "fs.h"
#include "fcntl.h"

#define MAX_LINE 120
#define MAX_PATH 80
#define MAX_OUTPUT 4096

static void
done(int id, char *status)
{
  printf(1, "@XV6 %d DONE %s\n", id, status);
}

// Encode payloads so file text cannot masquerade as a protocol response.
static void
data(int id, char *buf, int n)
{
  char hex[129];
  char *digits = "0123456789abcdef";
  int i;

  for(i = 0; i < n; i++){
    hex[2*i] = digits[((uchar)buf[i]) >> 4];
    hex[2*i+1] = digits[((uchar)buf[i]) & 15];
  }
  hex[2*n] = 0;
  printf(1, "@XV6 %d DATA %s\n", id, hex);
}

static int
validpath(char *path)
{
  int i, start, len;
  char c;

  len = strlen(path);
  if(len < 1 || len > MAX_PATH || path[0] != '/')
    return 0;
  if(len == 1)
    return 1;
  start = 1;
  for(i = 1; i <= len; i++){
    c = path[i];
    if(c == '/' || c == 0){
      if(i == start || i-start > DIRSIZ ||
         (i-start == 1 && path[start] == '.') ||
         (i-start == 2 && path[start] == '.' && path[start+1] == '.'))
        return 0;
      start = i+1;
    } else if(!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.'))
      return 0;
  }
  return 1;
}

static void
readfile(int id, int fd)
{
  char buf[64];
  int n, i, total, want;

  total = 0;
  while(total < MAX_OUTPUT){
    want = MAX_OUTPUT-total;
    if(want > sizeof(buf))
      want = sizeof(buf);
    n = read(fd, buf, want);
    if(n < 0){
      done(id, "ERROR read_failed");
      return;
    }
    if(n == 0){
      done(id, "OK");
      return;
    }
    for(i = 0; i < n; i++){
      if(((uchar)buf[i] < 32 && buf[i] != '\n' &&
          buf[i] != '\r' && buf[i] != '\t') || (uchar)buf[i] == 127){
        done(id, "ERROR not_text");
        return;
      }
    }
    data(id, buf, n);
    total += n;
  }
  n = read(fd, buf, 1);
  done(id, n < 0 ? "ERROR read_failed" : n ? "TRUNCATED" : "OK");
}

static void
listfiles(int id, int fd)
{
  struct dirent de;
  char name[DIRSIZ+2];
  int n, len, total;

  total = 0;
  while((n = read(fd, &de, sizeof(de))) == sizeof(de)){
    if(de.inum == 0)
      continue;
    memmove(name, de.name, DIRSIZ);
    name[DIRSIZ] = 0;
    if(strcmp(name, ".") == 0 || strcmp(name, "..") == 0)
      continue;
    len = strlen(name);
    name[len++] = '\n';
    if(total+len > MAX_OUTPUT){
      done(id, "TRUNCATED");
      return;
    }
    data(id, name, len);
    total += len;
  }
  done(id, n == 0 ? "OK" : "ERROR read_failed");
}

static void
request(char *line)
{
  char *op, *path, *p;
  int id, fd;
  struct stat st;

  op = strchr(line, ' ');
  if(op == 0 || op == line || op-line > 9){
    done(0, "ERROR invalid_request");
    return;
  }
  *op++ = 0;
  for(p = line; *p; p++)
    if(*p < '0' || *p > '9'){
      done(0, "ERROR invalid_request");
      return;
    }
  id = atoi(line);
  path = strchr(op, ' ');
  if(id < 1 || path == 0){
    done(id, "ERROR invalid_request");
    return;
  }
  *path++ = 0;
  if(!validpath(path)){
    done(id, "ERROR invalid_path");
    return;
  }
  if(strcmp(op, "READ") != 0 && strcmp(op, "LIST") != 0){
    done(id, "ERROR unknown_operation");
    return;
  }
  fd = open(path, O_RDONLY);
  if(fd < 0){
    done(id, "ERROR open_failed");
    return;
  }
  if(fstat(fd, &st) < 0)
    done(id, "ERROR stat_failed");
  else if(strcmp(op, "READ") == 0 && st.type == T_FILE)
    readfile(id, fd);
  else if(strcmp(op, "LIST") == 0 && st.type == T_DIR)
    listfiles(id, fd);
  else
    done(id, "ERROR wrong_file_type");
  close(fd);
}

int
main(void)
{
  char line[MAX_LINE], c;
  int n, overflow;

  printf(1, "@XV6 READY 1 %d\n", getpid());
  n = overflow = 0;
  while(read(0, &c, 1) == 1){
    if(c == '\n'){
      line[n] = 0;
      if(overflow)
        done(0, "ERROR request_too_long");
      else
        request(line);
      n = overflow = 0;
    } else if(n < sizeof(line)-1)
      line[n++] = c;
    else
      overflow = 1;
  }
  exit();
}
