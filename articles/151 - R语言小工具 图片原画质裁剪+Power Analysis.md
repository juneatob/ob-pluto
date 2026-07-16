---
title: "R语言小工具 | 图片原画质裁剪+Power Analysis"
account: "百味鸡OB Pluto"
author: "快乐百味鸡"
source: "https://mp.weixin.qq.com/s/JsxxLII9T_bZQmZSEmfJeQ"
exported_at: "2026-06-24T07:56:07.656Z"
order: 151
fetch_error: ""
tags:
  - wechat
  - 百味鸡ob-pluto
---
![](../assets/ee7874407d46b15ff0be.png)

Beginning

好久没有更新这个系列…

趁着最近论文中有用R进行一些工具性的处理，就来这分享一下代码。

也方便我自己之后可以直接打开公众号调用~

![](../assets/95d4398cb7372160d19a.png)

**PART 1**

**图片原画质裁剪**

**#介绍：我们在放论文图片的时候，很多时候会用直接截图的方法，但是截图很可能会压缩画质。用R语言就可以不用下载Ai ps这种软件，轻松对图片进行原画质裁剪！**

# 导入所需的包

library(pdftools)

library(magick)

# PDF文件路径

pdf_file_path <- "forest.pdf"

# 读取PDF文件

pdf <- pdf_render_page(pdf_file_path)

# 设置输出文件名和路径。在Mac上，'~' 符号代表用户主目录

output_file_path <- "~/Desktop/forest.jpg"

# 将PDF转换为高分辨率的PNG图像，以便在裁剪时保留透明度

png_image <- image_read_pdf(pdf_file_path, density = 600)

png_image <- image_convert(png_image, format = "png")

# 获取图像的宽度和高度

img_width <- image_info(png_image)$width

img_height <- image_info(png_image)$height

# 重新调整上半部分的高度（比如只取原画幅的上0.8）

crop_height <- img_height*0.8

# 裁剪上半部分

cropped_image <- image_crop(png_image, geometry = paste0(img_width, "x", crop_height, "+0+0"))

# 将PNG图像转换为JPEG格式

jpeg_image <- image_convert(cropped_image, format = "jpg")

# 保存上半部分的JPEG图像到桌面

image_write(jpeg_image, path = output_file_path)

**PART 2**

****事后检验样本量/power是否达到要求****

![](../assets/a275c47d33f41963df2a.png)

**#介绍：现在的论文一般都需要报告power以及样本量是否满足需求，如果是实验法就会用到Gpower，但是如果是复杂的结构方程模型，就可以用下面的代码很容易的实现！**

**ps: 这个基于RMSEA计算的方法比较简单，可以参考这篇文章的第二种：**

Jak, S., Jorgensen, T. D., Verdam, M. G. E., Oort, F. J., … Elffers, L. (2021). Analytical power calculations for structural equation modeling: A tutorial and Shiny app. *Behavior Research Methods*, *53*(4), 1385–1406. https://doi.org/10.3758/s13428-020-01479-0

# 导入所需的包

library(semTools)

#计算需要的样本量

findRMSEAsamplesize(rmsea0=0.07, rmseaA=0.052, df=58, power = 0.95, alpha = 0.05)

#但也可以根据样本量计算power；完整版可参考这篇推送

![](../assets/cdadfb03b1a20ab9313f.png)

Ending

以后我也多多更新这种小工具~
